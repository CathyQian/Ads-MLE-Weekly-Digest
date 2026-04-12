import os
import sys
import requests
import xml.etree.ElementTree as ET
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
ATOM_NS = "{http://www.w3.org/2005/Atom}"


def validate_env():
    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)


def summarize_with_gemini(abstract_text):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    data = {
        "contents": [{
            "parts": [{
                "text": (
                    "Summarize the following abstract in 1-2 simple sentences. "
                    "Focus on what the authors did, why, and the results:\n\n"
                    + abstract_text
                )
            }]
        }]
    }

    response = requests.post(url, headers=headers, json=data, timeout=30)
    if response.status_code == 200:
        try:
            return response.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as e:
            return f"Error: Unexpected Gemini response format: {e}"
    return f"Error: Gemini API returned {response.status_code}: {response.text}"


def fetch_papers_for_date_range(keyword, start_date, end_date, max_results):
    papers = []
    query = f'all:"{keyword}"'
    query_url = (
        f"https://export.arxiv.org/api/query?"
        f"search_query=({query})+AND+submittedDate:[{start_date}+TO+{end_date}]"
        f"&start=0&max_results={max_results}"
    )

    response = requests.get(query_url, timeout=30)
    if response.status_code != 200:
        print(f"Error: arXiv API returned {response.status_code} for keyword '{keyword}' ({start_date} to {end_date})")
        return papers

    root = ET.fromstring(response.content)
    for entry in root.findall(f"{ATOM_NS}entry"):
        title = entry.find(f"{ATOM_NS}title").text.strip().replace("\n", " ")
        summary = entry.find(f"{ATOM_NS}summary").text.strip()
        pdf_link = entry.find(f"{ATOM_NS}link[@title='pdf']")
        paper_id = entry.find(f"{ATOM_NS}id").text.strip()
        link = pdf_link.attrib["href"] if pdf_link is not None else paper_id.replace("/abs/", "/pdf/")
        papers.append({"title": title, "summary": summary, "link": link, "keyword": keyword})

    return papers


def fetch_papers(keywords, start_date, end_date, max_results_per_keyword):
    papers = []
    keyword_totals = {keyword: 0 for keyword in keywords}

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Split date range into ~30-day chunks to stay within arXiv API limits
    current_start = start_dt
    date_ranges = []
    while current_start < end_dt:
        current_end = min(current_start + timedelta(days=30), end_dt)
        date_ranges.append((current_start.strftime("%Y%m%d"), current_end.strftime("%Y%m%d")))
        current_start = current_end + timedelta(days=1)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fetch_papers_for_date_range, keyword, start, end, max_results_per_keyword): (keyword, start, end)
            for keyword in keywords
            for start, end in date_ranges
        }

        for future in as_completed(futures):
            keyword, start, end = futures[future]
            try:
                results = future.result()
                papers.extend(results)
            except Exception as e:
                print(f"Error fetching papers for '{keyword}' ({start} to {end}): {e}")

    for paper in papers:
        keyword_totals[paper["keyword"]] += 1

    print("\nTotal documents found per keyword:")
    for keyword, total in keyword_totals.items():
        print(f"  {keyword}: {total} documents")

    return papers


def main():
    validate_env()

    raw_keywords = input("Enter keywords separated by commas: ").strip()
    if not raw_keywords:
        print("Error: No keywords provided.")
        sys.exit(1)
    keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]
    if not keywords:
        print("Error: No valid keywords after parsing.")
        sys.exit(1)

    start_date = input("Enter start date (YYYY-MM-DD): ").strip()
    end_date = input("Enter end date (YYYY-MM-DD): ").strip()
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        print("Error: Dates must be in YYYY-MM-DD format.")
        sys.exit(1)

    max_results_input = input("Enter the number of results per keyword: ").strip()
    try:
        max_results_per_keyword = int(max_results_input)
        if max_results_per_keyword <= 0:
            raise ValueError
    except ValueError:
        print("Error: Number of results must be a positive integer.")
        sys.exit(1)

    papers = fetch_papers(keywords, start_date, end_date, max_results_per_keyword)

    with open("result.txt", "w") as result_file:
        for paper in papers:
            print(f"Summarizing: {paper['title']}")
            abstract = paper["summary"]
            summary = summarize_with_gemini(abstract)
            result_file.write(
                f"Keyword: {paper['keyword']}\n"
                f"Title: {paper['title']}\n"
                f"Link: {paper['link']}\n"
                f"Summary: {summary}\n\n"
            )
            print(f"Summary: {summary}\n")
            time.sleep(2)


if __name__ == "__main__":
    main()
