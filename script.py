import csv
import os
import pickle
import re
import time
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ['https://www.googleapis.com/auth/contacts']

def authenticate():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)

    return build('people', 'v1', credentials=creds)

def normalize_number(number):
    """Remove country code, hyphens, and spaces."""
    number = re.sub(r'\D', '', number)  # Remove non-digit characters
    if number.startswith('91') and len(number) > 10:
        number = number[-10:]  # Get last 10 digits (Indian mobile number)
    return number

def execute_with_retry(func, max_retries=5, initial_delay=2, max_delay=30):
    """
    Execute a function with retries and exponential backoff on HttpError 429.
    """
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            return func()
        except HttpError as e:
            if e.resp.status == 429:
                print(f"[!] Rate limit exceeded, retrying in {delay} seconds...")
                time.sleep(delay)
                delay = min(delay * 2, max_delay)
            else:
                raise
    raise Exception(f"Failed after {max_retries} retries due to rate limiting.")

def generate_search_variants(number):
    """
    Given a plain digit number (e.g., '1234567890'),
    generate different string formats to search in Google Contacts.
    """
    variants = set()

    # Plain digits
    variants.add(number)

    # +91 prefix with digits only
    variants.add('+91' + number)

    # Hyphen format: split as XXX-XXX-XXXX if length == 10
    if len(number) == 10:
        variants.add(f"{number[:3]}-{number[3:6]}-{number[6:]}")
    
    # Space format: split as XXXXX XXXXX if length == 10 or 11
    if len(number) == 10:
        variants.add(f"{number[:5]} {number[5:]}")
    elif len(number) == 11:
        variants.add(f"{number[:6]} {number[6:]}")
    
    return list(variants)
def search_contact(service, number):
    """
    Try searching contact using multiple formatted variants of the number.
    Returns a deduplicated list of contact results.
    """
    seen = set()
    all_results = []

    def add_unique_results(results):
        for result in results.get('results', []):
            res_id = result['person']['resourceName']
            if res_id not in seen:
                seen.add(res_id)
                all_results.append(result)

    variants = generate_search_variants(number)

    for variant in variants:
        try:
            results = execute_with_retry(
                lambda: service.people().searchContacts(
                    query=variant,
                    readMask='names,phoneNumbers'
                ).execute()
            )
            add_unique_results(results)
        except HttpError as e:
            print(f"[!] Search failed for {variant}: {e}")
            # optionally continue or break depending on error

    return all_results


def update_contacts(service, csv_path):
    updated_count = 0
    with open(csv_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)

        for row in reader:
            raw_old = row.get('Old Mobile No.', '').strip()
            raw_new = row.get('New Mobile No.', '').strip()

            if not raw_old or not raw_new:
                continue

            old = normalize_number(raw_old)
            new = normalize_number(raw_new)

            try:
                contacts = search_contact(service, old)
                if not contacts:
                    print(f"[!] {old} not found.")
                    continue

                for contact in contacts:
                    resource_name = contact['person']['resourceName']

                    full_contact = execute_with_retry(
                        lambda: service.people().get(
                            resourceName=resource_name,
                            personFields='phoneNumbers'
                        ).execute()
                    )

                    etag_value = full_contact['etag']
                    existing_numbers = full_contact.get('phoneNumbers', [])

                    found = any(normalize_number(pn['value']) == old for pn in existing_numbers)
                    if not found:
                        print(f"[~] Old number {old} not found in contact phone list.")
                        continue

                    new_phone_data = [{
                        'value': new,
                        'type': 'mobile'
                    }]

                    execute_with_retry(
                        lambda: service.people().updateContact(
                            resourceName=resource_name,
                            updatePersonFields='phoneNumbers',
                            body={
                                'etag': etag_value,
                                'phoneNumbers': new_phone_data
                            }
                        ).execute()
                    )

                    updated_count += 1
                    print(f"[✓] {old} → {new} | Updated count: {updated_count}")

                    # Delay to avoid hitting rate limits
                    time.sleep(2)

            except Exception as e:
                print(f"[x] Error updating {old}: {e}")

    print(f"\nTotal contacts updated: {updated_count}")

def main():
    service = authenticate()
    update_contacts(service, 'contacts.csv')

if __name__ == '__main__':
    main()
