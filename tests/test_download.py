"""Verify anonymous authentication, catalog filtering, audio decoding, and file output."""

import io
import json
import unittest
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import httpx2 as httpx
from typer.testing import CliRunner

from hlmv_dl import COGNITO_URL, SIGNER_URL, HelloMovie, Movie, app, decode_audio, is_valid_opus


# Identifiable header bytes of the retrieved Ogg Opus stream without audio packets
ENCODED_HEADER = bytes.fromhex(
    '4e66665201030101ed5ada76449aab9f2e4a137a7463e642057d35ce43a03fe86fbecdd6b0a5091d3990fa5ff015f9'
)
DECODED_HEADER = bytes.fromhex(
    '4f6767530002000000000000000099af2973000000000be9337601134f707573486561640101380180bb0000000000'
)
TOKEN = 1644344491
NOW = 1800000000


def movie() -> Movie:
    """Create a sample Movie instance matching the distribution server response format."""

    return Movie(
        title_id=1,
        name='Sample Title',
        lang_name='Japanese',
        contenttype=2,
        token=TOKEN,
        movie_show=NOW - 100,
        movie_start=NOW - 100,
        movie_end=NOW + 100,
        status_info=3,
        voiceguide_version=2,
        commentary_flag=True,
        external_bucket='original',
    )


class DownloadTest(unittest.TestCase):
    def test_decode_known_opus_header(self) -> None:
        self.assertEqual(decode_audio(ENCODED_HEADER, TOKEN), DECODED_HEADER)

    def test_wrong_token_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, 'Failed to restore'):
            decode_audio(ENCODED_HEADER, 0)

    def test_authentication_and_available_audio_filter(self) -> None:
        requested: list[str] = []
        available = movie()
        unavailable = [
            available.model_copy(update={'title_id': 2, 'contenttype': 1}),
            available.model_copy(update={'title_id': 3, 'movie_end': NOW - 1}),
            available.model_copy(update={'title_id': 4, 'movie_start': NOW + 1}),
            available.model_copy(update={'title_id': 5, 'movie_show': NOW + 1}),
            available.model_copy(update={'title_id': 6, 'status_info': 0}),
            available.model_copy(update={'title_id': 7, 'voiceguide_version': 0}),
        ]

        def respond(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if str(request.url) == COGNITO_URL:
                self.assertNotIn('Cookie', request.headers)
                return httpx.Response(200, json={'IdentityId': 'test-identity'})
            if str(request.url) == SIGNER_URL:
                self.assertNotIn('Cookie', request.headers)
                self.assertEqual(request.headers['IdentityId'], 'test-identity')
                self.assertEqual(json.loads(request.content), {'app_id': 'hellomovie'})
                return httpx.Response(
                    200,
                    headers={
                        'CloudFront-Policy': 'test-policy',
                        'CloudFront-Signature': 'test-signature',
                        'CloudFront-Key-Pair-Id': 'test-key',
                    },
                )
            self.assertIn('CloudFront-Policy=test-policy', request.headers['Cookie'])
            return httpx.Response(
                200,
                headers={'Date': format_datetime(datetime.fromtimestamp(NOW - 1000, UTC), usegmt=True), 'Age': '1000'},
                json={'movies': [item.model_dump() for item in [available, *unavailable]]},
            )

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            hello_movie = HelloMovie(client)
            self.assertEqual([item.title_id for item in hello_movie.list_movies()], [1])
            self.assertEqual([item.title_id for item in hello_movie.list_movies()], [1])
        self.assertEqual(requested.count(COGNITO_URL), 1)
        self.assertEqual(requested.count(SIGNER_URL), 1)

    def test_download_restores_audio_and_preserves_sync_package(self) -> None:
        package = io.BytesIO()
        with ZipFile(package, 'w') as archive:
            archive.writestr('voiceguide/data/sound.dat', ENCODED_HEADER)
            archive.writestr('voiceguide/db/packed2.db', b'fingerprint-data')
        requests: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            return httpx.Response(200, content=package.getvalue())

        with TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(respond)) as client:
            hello_movie = HelloMovie(client)
            hello_movie.authenticated_until = float('inf')
            output = Path(directory)
            (path,) = hello_movie.download(movie(), output, True)
            self.assertEqual(path.read_bytes(), DECODED_HEADER)
            self.assertEqual(path.with_suffix('.zip').read_bytes(), package.getvalue())
            saved_movie = Movie.model_validate_json(path.with_suffix('.info.json').read_text())
            self.assertEqual(saved_movie.token, TOKEN)
            with self.assertRaises(FileExistsError):
                hello_movie.download(movie(), output, True)
            self.assertEqual(len(requests), 1)
        self.assertEqual(requests, ['/v2/movie/1/2/voiceguide.zip'])

    def test_disney_path_and_audio_only_output(self) -> None:
        package = io.BytesIO()
        with ZipFile(package, 'w') as archive:
            archive.writestr('voiceguide/data/sound.dat', ENCODED_HEADER)

        def respond(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, '/movie/1/2/voiceguide.zip')
            return httpx.Response(200, content=package.getvalue())

        with TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(respond)) as client:
            hello_movie = HelloMovie(client)
            hello_movie.authenticated_until = float('inf')
            target = movie().model_copy(update={'external_bucket': 'hellomovie-app-disney', 'name': '../Sample'})
            (path,) = hello_movie.download(target, Path(directory), False)
            self.assertEqual(path.parent, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_download_saves_waiting_audio_by_default(self) -> None:
        encoded_waiting_header = bytes.fromhex(
            '4e66665201030101ed5ada76449aa2c2b11e137a746311e2218c35ce43a03fe86fbecdd6b0a6091d3990fa5ff015f9'
        )
        decoded_waiting_header = bytes.fromhex(
            '4f6767530002000000000000000090f2b62700000000fc49178701134f707573486561640102380180bb0000000000'
        )
        package = io.BytesIO()
        with ZipFile(package, 'w') as archive:
            archive.writestr('voiceguide/data/sound.dat', ENCODED_HEADER)
            archive.writestr('voiceguide/data/waiting.dat', encoded_waiting_header)

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=package.getvalue())

        with TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(respond)) as client:
            hello_movie = HelloMovie(client)
            hello_movie.authenticated_until = float('inf')
            output = Path(directory)
            audio_path, waiting_path = hello_movie.download(movie(), output, False)
            self.assertEqual(waiting_path, output / 'Sample Title [1].waiting.opus')
            self.assertEqual(audio_path.read_bytes(), DECODED_HEADER)
            self.assertEqual(waiting_path.read_bytes(), decoded_waiting_header)
            self.assertEqual(set(output.iterdir()), {audio_path, waiting_path})

    def test_invalid_list_and_id_combination_fails_before_network(self) -> None:
        result = CliRunner().invoke(app, ['--list', '1'])
        self.assertEqual(result.exit_code, 2)
        self.assertIn('Cannot specify both', result.output)

    def test_malformed_catalog_entry_is_skipped(self) -> None:
        """Skip entries that do not match the Movie schema."""

        available = movie().model_dump()
        malformed = {'title_id': 2, 'name': 'Broken'}
        markup = {'title_id': '[/red]', 'name': 'Looks like markup'}

        def respond(request: httpx.Request) -> httpx.Response:
            if str(request.url) == COGNITO_URL:
                return httpx.Response(200, json={'IdentityId': 'test-identity'})
            if str(request.url) == SIGNER_URL:
                return httpx.Response(
                    200,
                    headers={
                        'CloudFront-Policy': 'test-policy',
                        'CloudFront-Signature': 'test-signature',
                        'CloudFront-Key-Pair-Id': 'test-key',
                    },
                )
            return httpx.Response(
                200,
                headers={'Date': format_datetime(datetime.fromtimestamp(NOW, UTC), usegmt=True), 'Age': '0'},
                json={'movies': [available, malformed, markup]},
            )

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            hello_movie = HelloMovie(client)
            self.assertEqual([item.title_id for item in hello_movie.list_movies()], [1])

    def test_opus_header_is_validated_after_segment_table(self) -> None:
        """Locate the OpusHead packet after the Ogg segment table."""

        page = bytearray(64)
        page[:4] = b'OggS'
        page[26] = 2
        page[27:29] = bytes([19, 1])
        page[29:37] = b'OpusHead'
        self.assertTrue(is_valid_opus(bytes(page)))
        page[29:37] = b'NotOpus!'
        self.assertFalse(is_valid_opus(bytes(page)))

    def test_download_retries_once_after_authentication_error(self) -> None:
        """Refresh credentials and retry once when the package request returns 403."""

        package = io.BytesIO()
        with ZipFile(package, 'w') as archive:
            archive.writestr('voiceguide/data/sound.dat', ENCODED_HEADER)
        package_requests: list[str] = []
        identity_requests = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal identity_requests
            if str(request.url) == COGNITO_URL:
                identity_requests += 1
                return httpx.Response(200, json={'IdentityId': f'test-identity-{identity_requests}'})
            if str(request.url) == SIGNER_URL:
                return httpx.Response(
                    200,
                    headers={
                        'CloudFront-Policy': 'test-policy',
                        'CloudFront-Signature': 'test-signature',
                        'CloudFront-Key-Pair-Id': 'test-key',
                    },
                )
            package_requests.append(request.url.path)
            if len(package_requests) == 1:
                return httpx.Response(403)
            return httpx.Response(200, content=package.getvalue())

        with TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(respond)) as client:
            hello_movie = HelloMovie(client)
            (path,) = hello_movie.download(movie(), Path(directory), False)
            self.assertEqual(path.read_bytes(), DECODED_HEADER)
        self.assertEqual(package_requests, ['/v2/movie/1/2/voiceguide.zip'] * 2)
        self.assertEqual(identity_requests, 2)


if __name__ == '__main__':
    unittest.main()
