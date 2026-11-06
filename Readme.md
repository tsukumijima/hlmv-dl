# hlmv-dl

A tiny command-line tool to download audio guides and commentaries from HELLO! MOVIE and save them as Opus audio files.

> [!NOTE]  
> This is an unofficial, independent tool and is not affiliated with, endorsed by, or associated with any official service providers. All audio tracks, titles, and trademarks belong to their respective rights holders.  
> This software is provided "AS IS", without warranty of any kind. Use this tool entirely at your own risk. The author assumes no responsibility or liability for any consequences arising from its use.

## Features

- **Lossless Extraction**: Restores the audio directly into standard Ogg Opus without re-compression or generational quality loss.
- **Standalone**: No external dependencies or media processing tools (such as FFmpeg) required.
- **Batch Download**: Allows specifying multiple title IDs to download sequentially in a single run.
- **Package Preservation**: Optionally retains the original package archive and metadata with `--keep-package`.
- **Safe File Naming**: Automatically sanitizes title names to prevent filesystem path issues across operating systems.
- **Overwrite Protection**: Prevents accidental data loss by stopping if destination files already exist.

## Requirements

- Python 3.11 or higher
- [uv](https://github.com/astral-sh/uv) (recommended), [pipx](https://github.com/pypa/pipx), or standard `pip`

## Installation

### Method 1: Using `uv` (Recommended)

Install `hlmv-dl` directly into an isolated environment as a standalone command:

```bash
uv tool install git+https://github.com/tsukumijima/hlmv-dl.git
```

Or from a local repository clone:

```bash
git clone https://github.com/tsukumijima/hlmv-dl.git
cd hlmv-dl
uv tool install .
```

You can also run the tool on the fly without permanent installation using `uvx`:

```bash
uvx --from git+https://github.com/tsukumijima/hlmv-dl.git hlmv-dl --list
```

### Method 2: Using `pipx`

If you use `pipx`:

```bash
pipx install git+https://github.com/tsukumijima/hlmv-dl.git
```

### Method 3: Using standard `pip`

Inside a Python virtual environment:

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install the package
pip install git+https://github.com/tsukumijima/hlmv-dl.git
```

### Method 4: Running from Source for Development

```bash
git clone https://github.com/tsukumijima/hlmv-dl.git
cd hlmv-dl
uv sync
uv run hlmv-dl --help
```

## Usage

### Step 1: Listing Available Titles

Check which audio titles are currently active on the distribution server:

```bash
hlmv-dl --list
```

**Output format:**

```text
ID / Type / Language / Title
<ID> / Commentary / 日本語 / <Title Name>
<ID> / Audio Guide / 日本語 / <Title Name>
...
```

- **ID**: The unique numeric identifier used to download the title.
- **Type**: Either `Audio Guide` (音声ガイド) or `Commentary` (コメンタリー). Note that audio guides and commentaries for the same movie have separate Title IDs.
- **Language**: Language of the audio track (e.g. `日本語`).
- **Title**: Name of the movie or program.

> [!TIP]  
> You can filter the list using standard command-line tools:
> ```bash
> # Search for a specific title (case-insensitive)
> hlmv-dl --list | grep -i "Keyword"
> 
> # List only commentaries
> hlmv-dl --list | grep "Commentary"
> ```

> [!NOTE]  
> Only titles currently within their active distribution window are listed and available for download.

### Step 2: Downloading Audio

To download an audio title, pass its Title ID:

```bash
# Save to the current working directory
hlmv-dl <TITLE_ID>
```

#### Specifying an Output Directory

Use `-o` or `--output` to set the destination folder (it will be created automatically if it does not exist):

```bash
hlmv-dl <TITLE_ID> -o ./downloads
```

#### File Naming Pattern

Output audio files are saved using the following naming structure:

```text
<Sanitized Title> [<TITLE_ID>].opus
<Sanitized Title> [<TITLE_ID>].waiting.opus
```

The first file is the main track. The `.waiting.opus` file contains the pre-show waiting audio and is saved whenever the package includes that track.

Illegal filesystem characters (e.g., `/`, `\`, `:`, `*`, `?`, `"`, `<`, `>`, `|`) and control codes are automatically replaced with underscores (`_`) for safe cross-platform file saving.

#### Overwrite Protection

Existing files are never overwritten. If a file with the same destination name already exists, the download will abort with an error to protect existing data. To re-download, rename or remove the existing file first.

### Step 3: Downloading Multiple Titles in Batch

You can pass multiple Title IDs separated by spaces to download them sequentially:

```bash
hlmv-dl <ID_1> <ID_2> <ID_3> -o ./downloads
```

Duplicate IDs in the same command are automatically deduplicated.

### Step 4: Preserving Raw Packages and Metadata (`--keep-package`)

By default, temporary files are cleaned up immediately after extracting the Opus audio tracks.

If you want to keep the original raw package archive and title metadata, pass the `--keep-package` flag:

```bash
hlmv-dl <TITLE_ID> --keep-package -o ./downloads
```

This saves the following files in the output directory:

1. **`<Title> [<ID>].opus`**: Restored main audio track.
2. **`<Title> [<ID>].waiting.opus`**: Restored pre-show waiting audio, when included in the package.
3. **`<Title> [<ID>].zip`**: Original distribution archive containing the raw encrypted payload and synchronization data.
4. **`<Title> [<ID>].info.json`**: Formatted JSON file containing metadata returned by the catalog API (dates, bucket identifiers, tokens, etc.).

### Step 5: Restoring Audio from a Saved Package (`--decode`)

Titles leave the catalog once their screening period ends, and after that they can no
longer be downloaded. A package saved with `--keep-package` still contains everything
needed to restore the audio, because the metadata JSON next to it holds the token.

Use `--decode` to restore the Opus tracks from such a package:

```bash
hlmv-dl --decode "./downloads/<Title> [<ID>].zip"
```

The token is read from the `<Title> [<ID>].info.json` file beside the package. Pass
`--token` to supply it yourself when the metadata file is gone. The server is not
contacted at all, so this works for titles that are no longer listed.

## Command-Line Reference

```text
Usage: hlmv-dl [OPTIONS] [TITLE_IDS]...
```

### Arguments

| Argument | Description |
|---|---|
| `TITLE_IDS` | One or more Title IDs to download (as listed by `--list`) |

### Options

| Option | Short | Description | Default |
|---|---|---|---|
| `--list` | | List currently available audio titles and IDs | `False` |
| `--output` | `-o` | Destination directory for downloaded files | `.` (current dir) |
| `--keep-package` | | Also save the original package ZIP and metadata JSON | `False` |
| `--decode` | | Restore Opus files from a package saved with `--keep-package` | |
| `--token` | | Title token used by `--decode` (read from the metadata JSON when omitted) | |
| `--help` | | Show command help and exit | |

## Troubleshooting

- **`Title ID <ID> not found in available titles. Check --list.`**  
  The requested title is not currently active on the distribution server. Use `hlmv-dl --list` to check available titles.
- **`Destination file already exists: <path>`**  
  A file with the target name already exists. Specify a different output directory or move/remove the existing file.
- **`Cannot specify both --list and title IDs.`**  
  `--list` and download arguments cannot be used simultaneously. Run `--list` separately to find IDs first.

## Development

This project uses `uv` for package management and task automation.

```bash
# Install dependencies
uv sync

# Run linting and type checking (Ruff & Pyright)
uv run task lint

# Run unit tests
uv run task test

# Format source code
uv run task format
```

## License

[MIT License](License.txt)
