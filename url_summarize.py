import os
import sys
import re
import requests
import xml.etree.ElementTree as ET

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
ATOM_NS = "{http://www.w3.org/2005/Atom}"


def validate_env():
    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)


def fetch_abstract_from_api(arxiv_url):
    """Fetch paper title and abstract from the arXiv API using the paper ID in the URL."""
    match = re.search(r'arxiv\.org/abs/([^\s/?#]+)', arxiv_url)
    if not match:
        return None, None, f"Error: Could not extract arXiv ID from URL: {arxiv_url}"

    arxiv_id = match.group(1)
    api_url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
    response = requests.get(api_url, timeout=30)
    if response.status_code != 200:
        return None, None, f"Error: arXiv API returned {response.status_code} for {arxiv_url}"

    root = ET.fromstring(response.content)
    entry = root.find(f"{ATOM_NS}entry")
    if entry is None:
        return None, None, f"Error: Paper not found for ID {arxiv_id}"

    title = entry.find(f"{ATOM_NS}title").text.strip().replace("\n", " ")
    abstract = entry.find(f"{ATOM_NS}summary").text.strip()
    return title, abstract, None


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


def main():
    validate_env()

    print("Select an option:")
    print("1. Enter a single arXiv link")
    print("2. Provide a file with arXiv links (using 'links.txt')")

    option = input("Enter 1 or 2: ").strip()

    with open("result.txt", "w") as result_file:
        if option == "1":
            arxiv_url = input("Enter the arXiv URL: ").strip()
            print(f"Fetching abstract for: {arxiv_url}")
            title, abstract, error = fetch_abstract_from_api(arxiv_url)
            if error:
                print(error)
                result_file.write(f"arXiv URL: {arxiv_url}\nError: {error}\n\n")
            else:
                summary = summarize_with_gemini(abstract)
                result_file.write(f"arXiv URL: {arxiv_url}\nTitle: {title}\nSummary: {summary}\n\n")
                print(f"Summary for {arxiv_url}:\n{summary}\n")

        elif option == "2":
            try:
                with open("links.txt", "r") as f:
                    links = [line.strip() for line in f if line.strip()]
            except FileNotFoundError:
                print("Error: 'links.txt' not found.")
                result_file.write("Error: 'links.txt' not found.\n")
                return

            for arxiv_url in links:
                print(f"Fetching abstract for: {arxiv_url}")
                title, abstract, error = fetch_abstract_from_api(arxiv_url)
                if error:
                    print(error)
                    result_file.write(f"arXiv URL: {arxiv_url}\nError: {error}\n\n")
                else:
                    summary = summarize_with_gemini(abstract)
                    result_file.write(f"arXiv URL: {arxiv_url}\nTitle: {title}\nSummary: {summary}\n\n")
                    print(f"Summary for {arxiv_url}:\n{summary}\n")
        else:
            print("Invalid option. Please run the script again and choose 1 or 2.")


if __name__ == "__main__":
    main()
