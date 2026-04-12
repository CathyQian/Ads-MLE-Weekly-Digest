# ArXiv Paper Summarizer (Anthropic + Notion)

This repository provides Python scripts to fetch and summarize research papers from arXiv using Anthropic, then save them into Notion. It includes a GitHub Actions workflow that runs weekly on Friday night, searches arXiv for papers published in the past week matching your configured keywords, generates summaries, and stores them in a Notion database. The tool is designed to help researchers, students, and enthusiasts quickly extract key insights from arXiv papers without manually reading through lengthy documents.

## Features
- **Single URL Summarization**: Summarize a single arXiv paper by providing its URL.
- **Batch URL Summarization**: Summarize multiple arXiv papers by listing their URLs in a text file.
- **Batch Keywords Summarization**: Fetch and summarize all papers from arXiv based on keywords and date ranges.
- **Weekly Automation**: GitHub Actions workflow runs every Friday night, searches the past week's arXiv papers by keyword, and inserts new papers with summaries into Notion.
- **Anthropic API Integration**: Leverages Anthropic's `claude-3-5-haiku-latest` model for high-quality summarization.

## Prerequisites
- Python 3.11
- Conda (for environment management)
- An [Anthropic API key](https://console.anthropic.com/settings/keys)

## Installation

### 1. Clone the Repository
```bash
git clone https://github.com/Shaier/arxiv_summarizer.git
cd arxiv_summarizer
```

### 2. Set Up the Conda Environment
Create and activate a Conda environment with Python 3.11:
```bash
conda create -n arxiv_summarizer python=3.11
conda activate arxiv_summarizer
```

### 3. Install Dependencies
Install the required Python packages using pip:
```bash
pip install -r requirements.txt
```

### 4. Configure the Anthropic API Key
Obtain your API key from [Anthropic Console](https://console.anthropic.com/settings/keys), then set it as an environment variable:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Usage

### Summarize a Single Paper (Based on a Single URL)
To summarize a single arXiv paper, run the script and provide the arXiv URL (ensure it is the abstract page, not the PDF link):
```bash
python url_summarize.py
```
When prompted:
1. Enter `1` to summarize a single paper.
2. Provide the arXiv URL (e.g., `https://arxiv.org/abs/2410.08003`).

### Summarize Multiple Papers (Based on Multiple URLs)
To summarize multiple papers:
1. Add the arXiv URLs to the `links.txt` file, with one URL per line.
2. Run the script:
```bash
python url_summarize.py
```
3. When prompted, enter `2` to process all URLs listed in `links.txt`. Summaries are saved in `result.txt`.

## Example
Here's an example of how to use the script:
```bash
python url_summarize.py
> Enter 1 for single paper or 2 for multiple papers: 1
> Enter the arXiv URL: https://arxiv.org/abs/2410.08003
```

### Summarize Multiple Papers (Based on Keywords)

`keywords_summarizer.py` enables fetching and summarizing papers based on specified keywords and date ranges. This is useful for tracking new research trends, generating related work sections, or conducting systematic reviews across multiple keywords at once.

### Usage

1. **Run the script** and provide your search criteria:
```bash
python keywords_summarizer.py
```
2. **Specify keywords and a date range** when prompted. Example input:
```bash
Enter keywords: "transformer, sparsity, MoE"
Enter start date (YYYY-MM-DD): 2017-01-01
Enter end date (YYYY-MM-DD): 2024-03-01
```
3. The script fetches relevant papers from arXiv and generates summaries. The results are saved in `result.txt`.


## Automatic Weekly Summarization (GitHub Actions)

A GitHub Actions workflow runs `daily_runner.py` every Friday at 9 PM ET. It searches arXiv for papers published in the past 7 days matching keywords in `config.yaml`, summarizes them with Anthropic, and inserts them into Notion. Papers matching multiple keywords are deduplicated within each run.

### Notion Database Setup

1. Create a [Notion integration](https://www.notion.so/my-integrations) and copy the token.
2. Create a Notion database with these properties:

| Property | Type | Description |
|----------|------|-------------|
| Title | title | Paper title |
| URL | url | arXiv abstract link |
| PDF | url | arXiv PDF link |
| Summary | rich_text | Generated summary |
| Keyword | select | Matched keyword |
| Date | date | Date added |

3. Share the database with your integration (click **...** → **Connections** → add your integration).
4. Copy the **database ID** from the URL — it's the 32-character hex string after your workspace name and before `?v=`.

### GitHub Actions Setup (Anthropic)

1. Push this repo to GitHub.
2. Go to your repo's **Settings → Secrets and variables → Actions**.
3. Add these three repository secrets:
   - `ANTHROPIC_API_KEY` — your Anthropic API key
   - `NOTION_API_KEY` — your Notion integration token
   - `NOTION_DATABASE_ID` — the database ID from the step above
4. The workflow runs automatically every Friday at 9 PM ET. You can also trigger it manually from the **Actions** tab → **Weekly arXiv Anthropic Summarizer** → **Run workflow**.

### Configuring Keywords

Edit `config.yaml` to add, remove, or change keywords:
```yaml
keywords:
  - "language models"
  - "llm"
  - "transformer"

max_results_per_keyword: 10
```

Commit and push — the next run will use the updated keywords.

### Running Locally

You can also run the summarizer locally (it will scan the past 7 days):
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export NOTION_API_KEY="ntn_..."
export NOTION_DATABASE_ID="your-database-id"
python daily_runner.py
```


## Contributing
Contributions are welcome! If you have suggestions, improvements, or bug fixes, please open an issue or submit a pull request.

## Support
If you encounter any issues or have questions, feel free to open an issue.
