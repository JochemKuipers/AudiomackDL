## AudiomackDL

Downloads an Audiomack **track / album / playlist** URL using the RapidAPI endpoints you provided.

### Setup

- Create a virtualenv (optional) and install deps:

```bash
python -m pip install -U pip
python -m pip install -e .
```

- Set your RapidAPI key (recommended) and host:

```powershell
$env:RAPIDAPI_KEY="YOUR_KEY_HERE"
$env:RAPIDAPI_HOST="audiomack-scraper.p.rapidapi.com"
```

### Usage

Download into your default `Downloads/AudiomackDL`:

```bash
python main.py "https://audiomack.com/kendricklamar/song/not-like-us"
python main.py "https://audiomack.com/drake/album/for-all-the-dogs"
python main.py "https://audiomack.com/michael/playlist/rap-afrique"
```

Choose output directory:

```bash
python main.py "https://audiomack.com/drake/album/for-all-the-dogs" --out ".\my_downloads"
```

### Notes

- This uses **signed stream URLs** returned by the API (`/audiomack/song/{id}/play`). Those links can expire.
- Do **not** hardcode your RapidAPI key in source code; use `RAPIDAPI_KEY`.
