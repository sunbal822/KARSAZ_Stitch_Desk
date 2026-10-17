# KARSAZ Stitch Desk

An order assistant for a crochet business. Upload a customer's reference photo and message, pick the colours on the photo, and get a price, a ready date, a before/after preview, a reply draft and a quote card you can send.

## What it does

1. **Photo and message** – upload the customer's photo and paste what they wrote.
2. **Colour change?** – say whether the customer wants different colours.
3. **Pick the colours** – click each part of the photo and choose its colour (palettes, swatches or a colour picker).
4. **Size, deadline, details** – age or size, quantity, due date, extra changes.
5. **Quote** – price, ready date, before/after picture, a reply you can copy, and a downloadable quote card.

Other tabs:

- **Orders** – search and filter your orders, change status (Quoted, Pending, In progress, Done, Cancelled), see counts and the month's booked value. Overdue and at-risk orders are flagged.
- **Prices** – edit your own yarn cost, hourly rate, extra colour charge, markup, rounding, hours per day and spare days.

Prices and dates always come from the code and your settings, never from the AI model.

## Run it on your computer

Needs Python 3.10 or newer.

```bash
pip install -r requirements.txt
python app.py
```

Then open the address shown in the terminal (usually http://127.0.0.1:7860).

**Reading the customer's message with AI (optional).** On your own computer the app can use [Ollama](https://ollama.com) with the `gemma3:4b` model to read the photo and message:

```bash
ollama pull gemma3:4b
```

If Ollama is not running, the app still works and you fill in the details by hand.

**Smart selection** (clicking a part of the photo) downloads a model of about 375 MB on first use and needs a lot of memory. Untick "Smart selection" if the page freezes or the computer is small.

## Settings

| File | What it controls |
|---|---|
| `pricing_config.json` | Default prices, markup, rounding, working hours per day, size chart |
| `catalog.json` | Your items: yarn grams, hours, number of colours, category |
| `palettes.py` | Suggested colour palettes for each kind of item |
| `colors.py` | Colour names the app understands |
| `assets/sloth_logo.png` | The logo shown in the app and on quote cards |

To add an item, add a line to `catalog.json`. No code changes needed.

## Environment variables

| Variable | Meaning |
|---|---|
| `KARSAZ_ACCOUNTS=1` | Turns on sign-in. Each user gets their own private orders and prices. Without it the app runs for one person with no sign-in. |
| `KARSAZ_DATA` | Folder where accounts, orders and prices are stored. On a server, point this at a persistent disk. |
| `GRADIO_SERVER_NAME` | Set to `0.0.0.0` on a server |
| `GRADIO_SERVER_PORT` | Port to listen on (Render uses `10000`) |

## Project files

| File | Purpose |
|---|---|
| `app.py` | The web app (screens, tabs, styling) |
| `pipeline.py` | Turns the customer's request into a quote and reply draft |
| `quote.py` | Price and ready-date calculation |
| `extract.py` | Reads the photo and message with Ollama |
| `recolor_v5.py` | Part-by-part recolouring of the photo |
| `quotecard.py` | Draws the downloadable quote card |
| `accounts.py` | Optional sign-in and per-user storage |
| `orders.py` | Orders table, statuses and deadline checks |

## Deploying on Render

1. Push this repo to GitHub (keep it **private**, because it contains your prices).
2. In Render, create a **Web Service** from the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `python app.py`
3. Add a **persistent disk** (for example mounted at `/var/data`, 1 GB).
4. Set the environment variables: `KARSAZ_ACCOUNTS=1`, `KARSAZ_DATA=/var/data`, `GRADIO_SERVER_NAME=0.0.0.0`, `GRADIO_SERVER_PORT=10000`.
5. On a small server, untick "Smart selection" in the app. Ollama is not available on Render, so the message-reading step is skipped and details are entered by hand.

## Privacy

- Orders, accounts and preview pictures are kept out of Git by `.gitignore`.
- Preview pictures are deleted automatically after 12 hours.
- Passwords are stored in `data/users.json`. There is no password-reset feature yet.
