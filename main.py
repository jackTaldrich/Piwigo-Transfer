import base64
import csv
import io
import os
import requests

from dotenv import load_dotenv
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright, Page


class Colors:
    RED = '\033[91m'
    YELLOW = '\033[33m'
    RESET = '\033[0m'


# API
load_dotenv()
API_KEY = os.getenv('API_KEY')
if not API_KEY:
    raise RuntimeError('API Key not found')

ALT_API_KEY = os.getenv('ALT_TEXT_API')
if not ALT_API_KEY:
    raise RuntimeError('Alt API Key not found')

HEADERS = {'X-PIWIGO-API': API_KEY}
MAX_IMAGE_BYTES = 1_500_000


def load_ids(datafile):
    ids = set()
    with open(datafile, 'r') as f:
        reader = csv.reader(f, delimiter='\t')
        next(reader) # skip header

        for row in reader:
            ids.add(row[1])  # id column

    return ids


def load_lazy(page: Page):
    for i in range(2):
        page.keyboard.press('End')
        page.wait_for_timeout(250)
        page.keyboard.press('PageUp')
        page.wait_for_timeout(250)
        page.keyboard.press('PageUp')
        page.wait_for_timeout(250)
        page.keyboard.press('Home')
        page.wait_for_timeout(250)
        page.keyboard.press('PageDown')
        page.wait_for_timeout(250)
        page.keyboard.press('PageDown')
        page.wait_for_timeout(250)

    page.wait_for_timeout(250)


def api_post(method: str, data: dict, files=None):
    payload = {'method': method, **data}
    r = requests.post(
        'https://mines.piwigo.com/ws.php?format=json',
        headers=HEADERS,
        data=payload,
        files=files,
        timeout=30,
    )

    text = r.text or ''
    if r.status_code != 200:
        raise RuntimeError(f'HTTP {r.status_code} from Piwigo: {text[:300]}')

    try:
        js = r.json()
    except Exception:
        raise RuntimeError(f'Non-JSON response from Piwigo: {text[:300]}')

    if js.get('stat') == 'fail':
        raise RuntimeError(f'{method} failed: {js.get('err')} {js.get('message')}')

    return js['result']


def ensure_header(path, header, delimiter='\t'):
    path = Path(path)

    if not path.exists() or path.stat().st_size == 0:
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f, delimiter=delimiter)
            writer.writerow(header)


def compress_image_to_limit(image_path: str, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    img = Image.open(image_path)

    # Convert to RGB (JPEG safe)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Resize if huge
    max_dimension = 1600
    if max(img.size) > max_dimension:
        img.thumbnail((max_dimension, max_dimension))

    # Iteratively reduce quality
    quality = 85
    step = 5

    while quality >= 30:
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()

        if len(data) <= max_bytes:
            return data

        quality -= step

    raise ValueError("Unable to compress image below size limit.")


def alttext_from_file(image_path: str, api_key: str, lang="en", max_chars=125) -> str:
    compressed_bytes = compress_image_to_limit(image_path)

    b64 = base64.b64encode(compressed_bytes).decode("ascii")

    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "image": {
            "raw": b64,
            "lang": lang,
            "max_chars": max_chars,
        }
    }

    r = requests.post("https://alttext.ai/api/v1/images", headers=headers, json=payload, timeout=60)
    r.raise_for_status()

    data = r.json()
    alt_text = data.get("alt_text")

    if not alt_text:
        raise RuntimeError(f"No alt_text in response. Response: {data}")

    return alt_text


# TSV FILES
COMPLETED = 'completed.tsv'
FAILED = 'failed.tsv'

ensure_header(
    COMPLETED,
    ['LocalPath', 'DepositID', 'SourceURL', 'Title', 'Author', 'AltText', 'Keywords', 'PiwigoID']
)

print('Do not minimize the browser that opens, it will prevent some information from gathering.')
directory_raw = input('Directory: ').strip()
directory = Path(directory_raw).expanduser().resolve()

def main():
    total_photos = sum(1 for _ in directory.iterdir())

    if total_photos == 0:
        total_photos = -1

    with open(COMPLETED, 'a') as completed_tsv, open(FAILED, 'w') as failed_tsv:
        completed = csv.writer(completed_tsv, delimiter='\t')
        failed = csv.writer(failed_tsv, delimiter='\t')

        # header for write mode
        failed.writerow(['LocalPath', 'DepositID', 'Stage', 'Error'])

        # get completed ids
        completed_ids = load_ids(COMPLETED)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()

            photo_count = 0
            for filepath in directory.iterdir():
                photo_count += 1

                # skip other files
                file = filepath.name
                if not file.startswith('Depositphotos_') or not file.endswith('_XL.jpg'):
                    continue

                # get photo id/search url from filename
                deposit_id = file.removeprefix('Depositphotos_').removesuffix('_XL.jpg')
                search_url = 'https://depositphotos.com/search/' + deposit_id

                print(f'ID: {deposit_id}')

                if deposit_id in completed_ids:
                    print(f'{Colors.YELLOW}Already completed{Colors.RESET}\n')
                    continue

                # generate alt text for image
                alt_text = alttext_from_file(str(filepath), ALT_API_KEY)

                # pull info from deposit photos
                page.goto(search_url)

                deposit_url = page.url
                print(deposit_url)

                # title
                # there is only one h1 element so this is reliable
                try:
                    title_locator = page.locator('h1')
                    title_locator.wait_for(timeout=10_000)
                    title = title_locator.inner_text().strip()

                    if title.endswith(' — Photo'):
                        title = title.removesuffix(' — Photo')

                    elif title.endswith(' — Vector'):
                        title = title.removesuffix(' — Vector')

                    if title == 'Sorry, but we haven\'t found anything':
                        print(f'{Colors.RED}Could not find photo ID {deposit_id} on Deposit Photos search{Colors.RESET}')
                        failed.writerow([filepath, deposit_id, 'Gathering Title', 'Photo doesn\'t exist on deposit photos'])
                        continue

                    print(f'Title: {title}')
                except Exception as e:
                    failed.writerow([filepath, deposit_id, 'Gathering Title', str(e)])
                    continue

                # author
                try:
                    author_locator = page.locator('._wdeBj')
                    author_locator.wait_for(timeout=10_000)

                    author = author_locator.inner_text().strip()

                    if 'Photo by ' in author:
                        _, _, author = author.partition('Photo by ')
                    elif 'Vector by ' in author:
                        _, _, author = author.partition('Vector by ')

                    print(f'Author: {author}')
                except Exception as e:
                    failed.writerow([filepath, deposit_id, 'Gathering Author', str(e)])
                    continue

                # alt-text printing from before
                print(f'Alt Text: {alt_text}')

                # keywords
                seen = set()
                keywords = []

                load_lazy(page)

                try:
                    ul_locator = page.locator('._U57rH').last
                    keywords_locator = ul_locator.locator('li')
                    num_keywords = keywords_locator.count()

                    print('Keywords: ', end='')
                    for i in range(num_keywords):
                        li = keywords_locator.nth(i)
                        keyword = li.inner_text().strip().lower()
                        keywords.append(keyword)
                        seen.add(keyword)
                        print(keyword, end=', ')
                    print()
                except Exception as e:
                    failed.writerow([filepath, deposit_id, 'Gathering Keywords', str(e)])
                    continue

                # max amount is 50 keywords
                keywords = keywords[:50]
                keywords_cell = ';'.join(keywords)

                description = f"""
Alt Text: {alt_text}
Source URL: {deposit_url}
Publisher: DepositPhotos
Attribution: {author}/DepositPhotos
"""

                # Publish to Piwigo
                try:
                    tags = ','.join(keywords)
                    payload = {
                        'category': 3,
                        'name': title,
                        'author': author,
                        'comment': description,
                        'tags': tags,
                    }

                    with open(filepath, 'rb') as f:
                        result = api_post(
                            'pwg.images.addSimple',
                            payload,
                            files={'image': f},
                        )
                        piwigo_id = result['image_id']
                        print(f'Piwigo ID: {piwigo_id}')
                except Exception as e:
                    failed.writerow([filepath, deposit_id, 'Piwigo addSimple', str(e)])
                    print(f'{Colors.RED}File {filepath} failed uploading:{Colors.RESET} {str(e)}')
                    continue

                print(f'{photo_count / total_photos * 100:.1f}% complete, {photo_count}/{total_photos}')

                print() # separate photos in terminal

                # success
                completed_ids.add(deposit_id)
                completed.writerow([filepath, deposit_id, deposit_url, title, author, alt_text, keywords_cell, piwigo_id])

            browser.close()

if __name__ == '__main__':
    main()
