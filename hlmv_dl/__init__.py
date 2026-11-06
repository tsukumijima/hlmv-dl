import json
import re
import shutil
import time
import zlib
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import TemporaryFile
from typing import Annotated, Any, BinaryIO
from zipfile import BadZipFile, ZipFile

import httpx2 as httpx
import typer
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from rich.console import Console
from rich.progress import DownloadColumn, Progress, SpinnerColumn, TextColumn, TransferSpeedColumn


BASE_URL = 'https://app.hellomovie.app'
SIGNER_URL = 'https://app-signer.hellomovie.app/app_signer'
COGNITO_URL = 'https://cognito-identity.us-east-1.amazonaws.com/'
IDENTITY_POOL_ID = 'us-east-1:dbd8d29b-d3e3-48e9-9a9e-a8474060acab'
app = typer.Typer(add_completion=False, pretty_exceptions_enable=False, no_args_is_help=True)
console = Console()


class Identity(BaseModel):
    """Identifier issued by anonymous authentication."""

    identity_id: str = Field(alias='IdentityId')


class Movie(BaseModel):
    """Metadata and parameters required to retrieve and decode audio."""

    model_config = ConfigDict(extra='allow')

    title_id: int = Field(gt=0)
    name: str
    lang_name: str
    contenttype: int
    token: int
    movie_show: int
    movie_start: int
    movie_end: int
    status_info: int
    voiceguide_version: int
    commentary_flag: bool
    external_bucket: str


def parse_catalog(content: bytes) -> list[Movie]:
    """
    Parse the catalog payload and skip entries that do not match the expected schema.

    Args:
        content (bytes): Raw JSON response from the catalog endpoint

    Returns:
        list[Movie]: Valid titles in the order returned by the server
    """

    payload: Any = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError('Unexpected catalog format.')
    entries = payload.get('movies')
    if not isinstance(entries, list):
        raise ValueError('The catalog does not contain a movie list.')
    adapter: TypeAdapter[Movie] = TypeAdapter(Movie)
    movies: list[Movie] = []
    for entry in entries:
        try:
            movies.append(adapter.validate_python(entry))
        except ValidationError:
            title_id = entry.get('title_id', 'unknown') if isinstance(entry, dict) else 'unknown'
            # title_id comes from the server, so never let Rich interpret it as markup.
            console.print(f'Skipping a malformed catalog entry: {title_id}', markup=False, style='yellow')
    return movies


class HelloMovie:
    """Client to retrieve title catalogs and audio packages using anonymous authentication."""

    def __init__(self, client: httpx.Client) -> None:
        """
        Initialize the client with a caller-managed HTTP client and shared session cookies.

        Args:
            client (httpx.Client): HTTP client managed by the caller
        """

        self.client = client
        self.authenticated_until = 0.0

    def authenticate(self) -> None:
        """Obtain signed CloudFront cookies from an anonymous Cognito identity and reuse within validity period."""

        if time.monotonic() < self.authenticated_until:
            return
        response = self.client.post(
            COGNITO_URL,
            headers={
                'Content-Type': 'application/x-amz-json-1.1',
                'X-Amz-Target': 'AWSCognitoIdentityService.GetId',
            },
            json={'IdentityPoolId': IDENTITY_POOL_ID},
        )
        response.raise_for_status()
        identity = Identity.model_validate_json(response.content)
        response = self.client.post(
            SIGNER_URL,
            headers={'IdentityId': identity.identity_id},
            json={'app_id': 'hellomovie'},
        )
        response.raise_for_status()
        for name in ('CloudFront-Policy', 'CloudFront-Signature', 'CloudFront-Key-Pair-Id'):
            value = response.headers.get(name)
            if value is None:
                raise RuntimeError('Failed to obtain authorization cookies from the distribution server.')
            self.client.cookies.set(name, value, domain='app.hellomovie.app', path='/')
        # Refresh credentials before the 1-hour expiration during batch downloads
        self.authenticated_until = time.monotonic() + 50 * 60

    def list_movies(self) -> list[Movie]:
        """
        Retrieve audio guides and commentaries currently available for download.

        Returns:
            list[Movie]: Audio titles sorted by release date (newest first)
        """

        self.authenticate()
        response = self.client.get(f'{BASE_URL}/v2/movie/movies.json')
        response.raise_for_status()
        movies = parse_catalog(response.content)
        now = parsedate_to_datetime(response.headers['Date']).timestamp() + int(response.headers.get('Age', '0'))
        return sorted(
            (
                movie
                for movie in movies
                if movie.contenttype == 2
                and movie.status_info >= 1
                and movie.voiceguide_version > 0
                and movie.movie_show <= now
                and movie.movie_start <= now <= movie.movie_end
            ),
            key=lambda movie: (movie.movie_start, movie.title_id),
            reverse=True,
        )

    def _stream_package(self, url: str, package: BinaryIO) -> None:
        """
        Download an audio package into an open temporary file while showing progress.

        Args:
            url (str): Package URL
            package (BinaryIO): Destination temporary file
        """

        with (
            self.client.stream('GET', url) as response,
            Progress(
                SpinnerColumn(),
                TextColumn('{task.description}'),
                DownloadColumn(),
                TransferSpeedColumn(),
                console=console,
                disable=console.is_terminal is False,
            ) as progress,
        ):
            response.raise_for_status()
            total = int(response.headers['Content-Length']) if 'Content-Length' in response.headers else None
            task = progress.add_task('Downloading', total=total)
            for chunk in response.iter_bytes():
                package.write(chunk)
                progress.update(task, advance=len(chunk))

    def download(self, movie: Movie, output: Path, keep_package: bool) -> list[Path]:
        """
        Download an audio package and save the main track and any pre-show waiting track as Opus files.

        Args:
            movie (Movie): Target movie title
            output (Path): Destination directory
            keep_package (bool): Whether to also preserve the original ZIP package and metadata JSON

        Returns:
            list[Path]: Paths to the saved Opus files, with the main track first
        """

        name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', movie.name).rstrip(' .')
        stem = f'{name} [{movie.title_id}]'
        audio_path = output / f'{stem}.opus'
        waiting_path = output / f'{stem}.waiting.opus'
        package_path = output / f'{stem}.zip'
        metadata_path = output / f'{stem}.info.json'
        destinations = [audio_path, package_path, metadata_path] if keep_package is True else [audio_path]
        for path in destinations:
            if path.exists() is True:
                raise FileExistsError(f'Destination file already exists: {path}')

        prefix = {
            'original': 'v2/movie',
            'hellomovie-app-disney': 'movie',
            'hellomovie-app-deluxe-demo': 'deluxe',
        }.get(movie.external_bucket, 'movie')
        url = f'{BASE_URL}/{prefix}/{movie.title_id}/{movie.voiceguide_version}/voiceguide.zip'
        with TemporaryFile() as package:
            for attempt in range(2):
                try:
                    self.authenticate()
                    self._stream_package(url, package)
                    break
                except httpx.HTTPStatusError as ex:
                    if attempt == 0 and ex.response.status_code in (401, 403):
                        # Signed cookies may have expired between downloads; refresh and retry once.
                        self.authenticated_until = 0.0
                        package.seek(0)
                        package.truncate()
                        continue
                    raise
            package.seek(0)
            with ZipFile(package) as archive:
                # Read audio payload directly without extracting the entire archive
                audio_files = {audio_path: decode_audio(archive.read('voiceguide/data/sound.dat'), movie.token)}
                if 'voiceguide/data/waiting.dat' in archive.namelist():
                    if waiting_path.exists() is True:
                        raise FileExistsError(f'Destination file already exists: {waiting_path}')
                    audio_files[waiting_path] = decode_audio(archive.read('voiceguide/data/waiting.dat'), movie.token)
            output.mkdir(parents=True, exist_ok=True)
            for path, audio in audio_files.items():
                with path.open('xb') as audio_file:
                    audio_file.write(audio)
            if keep_package is True:
                package.seek(0)
                with package_path.open('xb') as package_file:
                    shutil.copyfileobj(package, package_file)
                with metadata_path.open('x', encoding='utf-8') as metadata_file:
                    metadata_file.write(movie.model_dump_json(indent=2) + '\n')
        return list(audio_files)


def is_valid_opus(data: bytes) -> bool:
    """
    Check that the payload starts with an Ogg page whose first packet is an OpusHead header.

    Args:
        data (bytes): Decoded audio payload

    Returns:
        bool: True when the payload looks like an Ogg Opus stream
    """

    if len(data) < 27 or data[:4] != b'OggS':
        return False
    segment_count = data[26]
    packet_start = 27 + segment_count
    return len(data) >= packet_start + 8 and data[packet_start : packet_start + 8] == b'OpusHead'


def decode_audio(encoded: bytes, token: int) -> bytes:
    """
    Reverse repetitive XOR encryption on sound.dat or waiting.dat to restore the original Ogg Opus stream.

    Args:
        encoded (bytes): Encoded sound.dat or waiting.dat payload from the package
        token (int): Title token from the catalog

    Returns:
        bytes: Bit-exact Ogg Opus audio data
    """

    # Reproduce 64-bit XorShift seeded with 32-bit initial value
    seed = ((((zlib.crc32(b'sound') ^ token) & 0xFFFFFFFFFFFFFFFF) >> 1) ^ 0xF229) & 0xFFFFFFFF
    states: list[int] = []
    for index in range(1, 5):
        seed = (1812433253 * (seed ^ (seed >> 30)) + index) & 0xFFFFFFFF
        states.append(seed)
    key = bytearray()
    for _ in range(2048):
        x, y, z, w = states
        temporary = (x ^ (x << 11)) & 0xFFFFFFFFFFFFFFFF
        value = (w ^ (w >> 19) ^ temporary ^ (temporary >> 8)) & 0xFFFFFFFFFFFFFFFF
        states = [y, z, w, value]
        key.append(int(1.0 + (float(value) * 253.0) * (2.0**-64)))
    decoded = bytes(value ^ key[index % 2048] for index, value in enumerate(encoded))
    if is_valid_opus(decoded) is False:
        raise RuntimeError('Failed to restore Ogg Opus stream. Please verify the source format.')
    return decoded


def decode_package(package: Path, token: int, output: Path) -> list[Path]:
    """
    Restore Opus files from a package that was saved with --keep-package.

    Args:
        package (Path): Package ZIP saved with --keep-package
        token (int): Title token used to decode the payload
        output (Path): Destination directory

    Returns:
        list[Path]: Paths to the restored Opus files, with the main track first
    """

    stem = package.stem
    with ZipFile(package) as archive:
        names = archive.namelist()
        if 'voiceguide/data/sound.dat' not in names:
            raise RuntimeError('The package does not contain the main audio track.')
        audio_files = {output / f'{stem}.opus': decode_audio(archive.read('voiceguide/data/sound.dat'), token)}
        if 'voiceguide/data/waiting.dat' in names:
            audio_files[output / f'{stem}.waiting.opus'] = decode_audio(
                archive.read('voiceguide/data/waiting.dat'), token
            )
    output.mkdir(parents=True, exist_ok=True)
    for path, audio in audio_files.items():
        with path.open('xb') as audio_file:
            audio_file.write(audio)
    return list(audio_files)


@app.command()
def download(
    title_ids: Annotated[list[int] | None, typer.Argument(help='Title IDs displayed in --list (multiple allowed)')] = None,
    list_titles: Annotated[bool, typer.Option('--list', help='List available audio titles and IDs')] = False,
    output: Annotated[
        Path | None, typer.Option('--output', '-o', help='Output directory for downloaded files')
    ] = None,
    keep_package: Annotated[
        bool, typer.Option('--keep-package', help='Also save the original package ZIP and metadata JSON')
    ] = False,
    decode_package_path: Annotated[
        Path | None,
        typer.Option(
            '--decode',
            help='Restore Opus files from a package saved with --keep-package instead of downloading',
        ),
    ] = None,
    token: Annotated[
        int | None, typer.Option('--token', help='Title token for --decode (read from the metadata JSON when omitted)')
    ] = None,
) -> None:
    """Download audio guides and commentaries from HELLO! MOVIE and save as Opus."""

    if decode_package_path is not None:
        if title_ids or list_titles is True:
            raise typer.BadParameter('Cannot specify --decode together with title IDs or --list.')
        info_path = decode_package_path.with_suffix('.info.json')
        if token is None:
            if info_path.exists() is False:
                raise typer.BadParameter(
                    f'Token not found. Pass --token or keep {info_path.name} beside the package.'
                )
            token = int(json.loads(info_path.read_text(encoding='utf-8'))['token'])
        for path in decode_package(decode_package_path, token, output if output is not None else decode_package_path.parent):
            console.print(f'Saved: {path.resolve()}', markup=False, soft_wrap=True)
        return
    if list_titles is True and title_ids:
        raise typer.BadParameter('Cannot specify both --list and title IDs.')
    if list_titles is False and not title_ids:
        raise typer.BadParameter('Please specify title IDs or use --list to see available titles.')
    with httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=True) as client:
        hello_movie = HelloMovie(client)
        movies = hello_movie.list_movies()
        if list_titles is True:
            console.print('ID / Type / Language / Title')
            for movie in movies:
                kind = 'Commentary' if movie.commentary_flag is True else 'Audio Guide'
                console.print(
                    f'{movie.title_id} / {kind} / {movie.lang_name} / {movie.name}', markup=False, soft_wrap=True
                )
            return
        selected: list[Movie] = []
        for title_id in dict.fromkeys(title_ids or []):
            movie = next((movie for movie in movies if movie.title_id == title_id), None)
            if movie is None:
                raise typer.BadParameter(f'Title ID {title_id} not found in available titles. Check --list.')
            selected.append(movie)
        for movie in selected:
            console.print(f'{movie.name} / {movie.lang_name} / ID: {movie.title_id}', markup=False, soft_wrap=True)
            for path in hello_movie.download(movie, output if output is not None else Path('.'), keep_package):
                console.print(f'Saved: {path.resolve()}', markup=False, soft_wrap=True)


def main() -> None:
    try:
        app()
    except httpx.HTTPStatusError as ex:
        console.print(f'[red]Server returned HTTP {ex.response.status_code}.[/red]')
        raise SystemExit(1) from None
    except httpx.RequestError:
        console.print('[red]Failed to connect to the distribution server. Please check your network connection.[/red]')
        raise SystemExit(1) from None
    except (RuntimeError, OSError, ValueError, BadZipFile, KeyError) as ex:
        console.print(f'Error: {ex}', markup=False, style='red')
        raise SystemExit(1) from None
