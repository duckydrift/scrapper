import requests
from bs4 import BeautifulSoup
import json
import time
from urllib.parse import urljoin

BASE_URL = "https://docs.oracle.com/en/cloud/saas/human-resources/oedmh/"
TOC_URL = "https://docs.oracle.com/en/cloud/saas/human-resources/oedmh/toc.htm"

def get_soup(url):
    try:
        response = requests.get(url)
        response.raise_for_status()
        return BeautifulSoup(response.content, 'html.parser')
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

def scrape_oracle_docs():
    print(f"Starting scrape of {TOC_URL}")
    
    # Derive BASE_URL from TOC_URL if not set or to ensure consistency
    # This assumes TOC is at the root of the documentation folder we want to scrape
    base_url_dynamic = TOC_URL.rsplit('/', 1)[0] + '/'
    print(f"Using Base URL: {base_url_dynamic}")
    
    soup = get_soup(TOC_URL)
    if not soup:
        return

    data = {}
    links_to_visit = []
    
    # Extract links from TOC
    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.get_text(strip=True).upper()
        
        # Skip common non-content links
        if 'index.html' in href or 'get-help' in href or 'toc.htm' in href:
            continue
            
        full_url = urljoin(base_url_dynamic, href)
        
        # Ensure we stay within the documentation set
        if full_url not in links_to_visit and full_url.startswith(base_url_dynamic):
            # Exclude anchor-only links to the base page
            if full_url == base_url_dynamic or full_url.startswith(base_url_dynamic + "#"):
                continue
            links_to_visit.append(full_url)

    print(f"Found {len(links_to_visit)} tables to scrape.")
    
    # No limit for full scrape
    # links_to_visit = links_to_visit[:10] 

    for i, url in enumerate(links_to_visit):
        print(f"[{i+1}/{len(links_to_visit)}] Scraping {url}...")
        page_soup = get_soup(url)
        if not page_soup:
            continue
            
        # Extract Table Name
        table_name_tag = page_soup.find('h1')
        table_name = table_name_tag.get_text(strip=True) if table_name_tag else ""
        if not table_name:
            title_tag = page_soup.find('title')
            if title_tag:
                table_name = title_tag.get_text().split('-')[0].strip()
        
        # Clean table name
        table_name = table_name.replace("Table: ", "").replace("View: ", "").strip()
        
        # Extract Description
        description = "Description not found"
        if table_name_tag:
            # Try next paragraph
            next_elem = table_name_tag.find_next_sibling('p')
            if not next_elem:
                # Try finding first p after h1
                next_elem = table_name_tag.find_next('p')
            
            if next_elem:
                description = next_elem.get_text(strip=True)

        columns = []
        
        # Find the columns table
        # Look for headers "Name", "Data Type"
        target_table = None
        tables = page_soup.find_all('table')
        
        for table in tables:
            headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            if ('name' in headers or 'column' in headers) and any('type' in h for h in headers):
                target_table = table
                break
        
        # Fallback for Views (single column "Name" table)
        if not target_table:
            for table in tables:
                headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
                if len(headers) == 1 and headers[0] == 'name':
                    target_table = table
                    break

        if target_table:
            # Map headers to indices
            headers = [th.get_text(strip=True).lower() for th in target_table.find_all('th')]
            
            name_idx = -1
            type_idx = -1
            null_idx = -1
            
            for i, h in enumerate(headers):
                if h == 'name' or h == 'column': name_idx = i
                elif 'type' in h: type_idx = i
                elif 'null' in h: null_idx = i
            
            if name_idx != -1 and type_idx != -1:
                # Standard Table Logic
                rows = target_table.find_all('tr')
                for row in rows:
                    # Skip header row
                    if row.find('th'): continue
                    
                    cols = row.find_all('td')
                    if not cols: continue
                    
                    # Ensure we have enough columns
                    if len(cols) > max(name_idx, type_idx):
                        col_name = cols[name_idx].get_text(strip=True)
                        data_type = cols[type_idx].get_text(strip=True)
                        nullable = "N"
                        
                        if null_idx != -1 and len(cols) > null_idx:
                            null_text = cols[null_idx].get_text(strip=True).lower()
                            if 'yes' in null_text or 'y' == null_text:
                                nullable = 'Y'
                        
                        if col_name and data_type:
                            columns.append({
                                "name": col_name,
                                "dataType": data_type,
                                "nullable": nullable
                            })
            elif name_idx != -1 and len(headers) == 1:
                # View Logic (Single cell with newlines)
                rows = target_table.find_all('tr')
                if len(rows) >= 2:
                    # Get the content of the first data row (index 1)
                    cols = rows[1].find_all('td')
                    if cols:
                        content = cols[0].get_text()
                        lines = [l.strip() for l in content.split('\n') if l.strip()]
                        
                        for line in lines:
                            columns.append({
                                "name": line,
                                "dataType": "View Column",
                                "nullable": "Unknown"
                            })

        if columns:
            data[table_name] = {
                "description": description,
                "columns": columns
            }
            print(f"  Extracted {table_name} with {len(columns)} columns.")
            
            # Save incrementally to ensure data is stored after each table is read
            try:
                with open('oracle_data.json', 'w') as f:
                    json.dump(data, f, indent=4)
            except Exception as e:
                print(f"Error saving data: {e}")
        else:
            print(f"  No columns found for {table_name}")

    # Final save (redundant but safe)
    with open('oracle_data.json', 'w') as f:
        json.dump(data, f, indent=4)
    
    print("Scraping complete. Data saved to oracle_data.json")

if __name__ == "__main__":
    scrape_oracle_docs()
