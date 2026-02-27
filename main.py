import csv
import os
import requests

from pathlib import Path
from dotenv import load_dotenv
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

HEADERS = {'X-PIWIGO-API': API_KEY}


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
    file_too_large = False

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

                if os.path.getsize(filepath) >= 19_500_000:
                    file_too_large = True
                    print(f'{Colors.RED}ID {deposit_id} is too large to generate alt text. Shrink file and run again{Colors.RESET}')
                    failed.writerow([filepath, deposit_id, 'Generating alt text', 'File is too large to generate alt text. Shrink file and run again'])
                    continue

                # generate alt text for image
                try:
                    page.goto(
                        'https://www.tailwindapp.com/marketing/tools/image-alt-text-generator',
                        wait_until='domcontentloaded',
                    )

                    page.wait_for_selector('input[type="file"]', timeout=10_000)
                    page.locator('input[type="file"]').set_input_files(str(filepath))

                    alt_text_locator = page.locator('textarea')
                    alt_text_locator.wait_for(timeout=10_000)

                    alt_text = alt_text_locator.input_value().strip()
                except Exception as e:
                    print(f'{Colors.RED}ID {deposit_id} failed to generate alt text{Colors.RESET}')
                    failed.writerow([filepath, deposit_id, 'Generating Alt Text', str(e)])
                    continue

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

            if file_too_large:
                print('One or more files was too large. Check the failed.tsv file to see which ones.')
                print('https://compressjpeg.com/')

            browser.close()

if __name__ == '__main__':
    main()
