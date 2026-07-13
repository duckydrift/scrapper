import requests
from bs4 import BeautifulSoup
import json
import os
import tempfile
import time
from urllib.parse import urljoin

DOC_ROOT = "https://docs.oracle.com/en/cloud/saas/"

# Oracle Fusion documentation sets, keyed by a short domain name. The value is
# the path segment under DOC_ROOT (product/version/doc-set); bump the version
# here when Oracle publishes a new release. Each domain writes to its own files
# (see out_files) so runs never clobber each other.
DOMAINS = {
    "financials": "financials/26c/oedmf",
    "common":     "applications-common/26c/oedma",
    "proc":       "procurement/26c/oedmp",
    "hcm":        "human-resources/oedmh",
    "ppm":        "project-management/26c/oedpp",
}

# Optional table-name prefix filter per domain. applications-common is huge and
# spans many products; the FuiSQL extension only needs the FND_* foundation
# objects, so we scrape only those for "common".
DOMAIN_FILTERS = {
    "common": "FND",
}


def domain_base_url(domain):
    return DOC_ROOT + DOMAINS[domain].rstrip("/") + "/"


def out_files(domain):
    """(data_file, relationships_file) for a domain. `financials` keeps the
    historical unsuffixed names that build_erd_data.py defaults to."""
    if domain == "financials":
        return "oracle_data.json", "relationships.json"
    return f"oracle_data_{domain}.json", f"relationships_{domain}.json"


def atomic_write_json(path, obj):
    """Write JSON to a temp file in the same dir, then os.replace into place.
    The replace is atomic, so a kill/sleep mid-write can never leave a
    truncated/corrupt file — readers see either the old or the new whole file."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def get_soup(url, retries=3, timeout=30):
    for attempt in range(retries):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            return BeautifulSoup(response.content, 'html.parser')
        except Exception as e:
            print(f"Error fetching {url} (attempt {attempt + 1}/{retries}): {e}")
            time.sleep(2 * (attempt + 1))
    return None


def header_set(table):
    """Lower-cased header labels of a <table>."""
    return [th.get_text(strip=True).lower() for th in table.find_all('th')]


def data_rows(table):
    """Yield lists of cell-text for each non-header row of a <table>."""
    for tr in table.find_all('tr'):
        if tr.find('th'):
            continue
        cells = tr.find_all('td')
        if cells:
            yield [td.get_text(strip=True) for td in cells]


def parse_columns(soup):
    """Columns table: Name / Datatype / Length / Precision / Not-null / ..."""
    # Disambiguate from the Primary Key table (also Name/Columns) by requiring a
    # datatype column.
    table = None
    for t in soup.find_all('table'):
        hs = header_set(t)
        if any(h in ("name", "column") for h in hs) and any("datatype" in h or "data type" in h for h in hs):
            table = t
            break

    columns = []
    if not table:
        # Fallback: view pages list columns in a single "Name" cell, one per
        # <p> element. get_text(strip=True) would concatenate them into one
        # blob, so pull each cell's text with a newline separator and split.
        for t in soup.find_all('table'):
            hs = header_set(t)
            if len(hs) == 1 and hs[0] == 'name':
                for tr in t.find_all('tr'):
                    if tr.find('th'):
                        continue
                    for td in tr.find_all('td'):
                        for line in (l.strip() for l in td.get_text('\n', strip=True).split('\n') if l.strip()):
                            columns.append({"name": line, "dataType": "View Column", "nullable": "Unknown"})
                break
        return columns

    hs = header_set(table)
    idx = {}
    for i, h in enumerate(hs):
        if h in ('name', 'column'):
            idx['name'] = i
        elif 'datatype' in h or 'data type' in h:
            idx['type'] = i
        elif 'not-null' in h or 'nullable' in h or h == 'not null':
            idx['notnull'] = i

    if 'name' not in idx or 'type' not in idx:
        return columns

    for row in data_rows(table):
        if len(row) <= max(idx['name'], idx['type']):
            continue
        name = row[idx['name']]
        dtype = row[idx['type']]
        if not name or not dtype:
            continue
        # "Not-null" column == "Yes" means the column is NOT nullable.
        nullable = "Y"
        if 'notnull' in idx and idx['notnull'] < len(row):
            if 'yes' in row[idx['notnull']].lower():
                nullable = "N"
        columns.append({"name": name, "dataType": dtype, "nullable": nullable})
    return columns


def parse_primary_key(soup):
    """Primary Key table: Name / Columns. Returns {name, columns:[...]}."""
    for t in soup.find_all('table'):
        hs = header_set(t)
        if hs == ['name', 'columns'] or (len(hs) == 2 and 'name' in hs and 'columns' in hs):
            for row in data_rows(t):
                if len(row) >= 2:
                    pk_cols = [c.strip().upper() for c in row[1].split(',') if c.strip()]
                    return {"name": row[0], "columns": pk_cols}
            return None
    return None


def parse_foreign_keys(soup, this_table):
    """Foreign Keys table: Table / Foreign Table / Foreign Key Column.

    Each row means: `Table` (a child) references `Foreign Table` (a parent) via
    `Foreign Key Column`. On a given page, `Foreign Table` is usually this table,
    so these are *incoming* references. We normalize table names to UPPER to
    match the dict keys / node ids used elsewhere.
    """
    table = None
    for t in soup.find_all('table'):
        hs = header_set(t)
        if any('foreign table' in h for h in hs) and any('foreign key column' in h for h in hs):
            table = t
            break
    if not table:
        return []

    fks = []
    for row in data_rows(table):
        if len(row) < 3:
            continue
        child, parent, column = row[0].strip().upper(), row[1].strip().upper(), row[2].strip().upper()
        if child and parent and column:
            fks.append({"fromTable": child, "toTable": parent, "column": column})
    return fks


def get_table_name(soup):
    tag = soup.find('h1')
    name = tag.get_text(strip=True) if tag else ""
    if not name:
        title = soup.find('title')
        if title:
            name = title.get_text().split('-')[0].strip()
    return name.replace("Table:", "").replace("View:", "").strip().upper()


def get_description(soup):
    # The description lives inside <div class="body">, either as
    # <p class="shortdesc"> or the first non-empty <p class="p"> before the
    # "Details" <section>. The bare "first <p> after <h1>" is empty here.
    body = soup.find('div', class_='body')
    if body:
        short = body.find('p', class_='shortdesc')
        if short and short.get_text(strip=True):
            return short.get_text(strip=True)
        for p in body.find_all('p', recursive=False):
            text = p.get_text(strip=True)
            if text:
                return text
        # Some pages nest the intro paragraph; take the first non-empty <p>
        # that is not inside a <section>/<table>/<ul>.
        for p in body.find_all('p'):
            if p.find_parent(['section', 'table', 'ul']):
                continue
            text = p.get_text(strip=True)
            if text:
                return text
    h1 = soup.find('h1')
    if h1:
        p = h1.find_next_sibling('p') or h1.find_next('p')
        if p:
            return p.get_text(strip=True)
    return ""


def scrape_oracle_docs(base_url, output_file, rel_file, name_prefix=None):
    base = base_url if base_url.endswith('/') else base_url + '/'
    toc_url = base + 'toc.htm'
    prefix = name_prefix.upper() if name_prefix else None
    print(f"Starting scrape of {toc_url}")
    print(f"Using Base URL: {base}" + (f"  (filter: names starting with {prefix})" if prefix else ""))

    soup = get_soup(toc_url)
    if not soup:
        return

    links_to_visit = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'index.html' in href or 'get-help' in href or 'toc.htm' in href:
            continue
        full_url = urljoin(base, href).split('#')[0]
        if not full_url.startswith(base) or full_url == base or full_url in links_to_visit:
            continue
        # Optimization: the page slug is the table name lower-cased with
        # separators removed (FND_ATTACHED_DOCUMENTS -> fndattacheddocuments-…),
        # so we can skip non-matching pages without fetching them.
        if prefix and not full_url.rsplit('/', 1)[-1].lower().startswith(prefix.lower()):
            continue
        links_to_visit.append(full_url)

    print(f"Found {len(links_to_visit)} pages to scrape.")

    # Resume: load any existing valid output and skip URLs already scraped.
    data = {}
    done_urls = set()
    if os.path.exists(output_file):
        try:
            with open(output_file) as f:
                data = json.load(f)
            print(f"Resuming: {len(data)} tables already in {output_file}.")
        except Exception as e:
            print(f"Existing {output_file} unreadable ({e}); starting fresh.")
            data = {}

    # When a prefix is active, drop any previously-scraped rows that don't match
    # (e.g. an older unfiltered run), so the output stays prefix-only.
    if prefix:
        kept = {k: v for k, v in data.items() if k.startswith(prefix)}
        if len(kept) != len(data):
            print(f"  Filtered existing data to {len(kept)} {prefix}* tables (was {len(data)}).")
        data = kept
    done_urls = {v["_sourceUrl"] for v in data.values()
                 if isinstance(v, dict) and v.get("_sourceUrl")}

    for i, url in enumerate(links_to_visit):
        if url in done_urls:
            continue
        print(f"[{i + 1}/{len(links_to_visit)}] Scraping {url}...")
        page = get_soup(url)
        if not page:
            continue

        table_name = get_table_name(page)
        if not table_name:
            continue
        if prefix and not table_name.startswith(prefix):
            continue

        columns = parse_columns(page)
        primary_key = parse_primary_key(page)
        foreign_keys = parse_foreign_keys(page, table_name)

        if not columns and not foreign_keys:
            print(f"  No data found for {table_name}")
            continue

        data[table_name] = {
            "description": get_description(page),
            "columns": columns,
            "primaryKey": primary_key,
            "foreignKeys": foreign_keys,
            "_sourceUrl": url,
        }
        print(f"  {table_name}: {len(columns)} cols, "
              f"PK={'yes' if primary_key else 'no'}, {len(foreign_keys)} FK rows")

        # Atomic, periodic save — resumable and immune to mid-write corruption.
        if len(data) % 20 == 0:
            atomic_write_json(output_file, data)

    atomic_write_json(output_file, data)

    # Dedupe and emit the flat relationship/edge list, derived from all scraped
    # tables (works correctly across resumes), keeping only intra-set edges.
    seen = set()
    edges = []
    for info in data.values():
        for r in info.get("foreignKeys", []) or []:
            key = (r["fromTable"], r["toTable"], r["column"])
            if key not in seen and r["fromTable"] in data and r["toTable"] in data:
                seen.add(key)
                edges.append(r)
    atomic_write_json(rel_file, edges)

    print(f"\nScraping complete. {len(data)} tables -> {output_file}")
    print(f"{len(edges)} unique relationships -> {rel_file}")


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Scrape Oracle Fusion table/view docs into a JSON data dictionary.",
        epilog="Examples:\n"
               "  python scraper.py financials\n"
               "  python scraper.py all\n"
               "  python scraper.py --base-url https://docs.oracle.com/.../oedmX/ --out oracle_data_x.json",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("domain", nargs="?",
                        help="Domain to scrape: " + ", ".join(DOMAINS) + ", or 'all'.")
    parser.add_argument("--base-url", help="Ad-hoc doc-set base URL for a domain not in DOMAINS.")
    parser.add_argument("--out", help="Output data file for --base-url (default oracle_data_custom.json).")
    parser.add_argument("--prefix", help="Only keep tables/views whose name starts with this "
                                         "(overrides a domain's default filter; use '' to disable).")
    parser.add_argument("--list", action="store_true", help="List known domains and exit.")
    args = parser.parse_args()

    if args.list:
        for k in DOMAINS:
            filt = DOMAIN_FILTERS.get(k)
            print(f"  {k:12} {domain_base_url(k)}" + (f"  [{filt}* only]" if filt else ""))
        return

    if args.base_url:
        data_file = args.out or "oracle_data_custom.json"
        rel = (data_file.replace("oracle_data", "relationships")
               if "oracle_data" in data_file else "relationships_custom.json")
        scrape_oracle_docs(args.base_url, data_file, rel, name_prefix=args.prefix or None)
        return

    if args.domain == "all":
        targets = list(DOMAINS)
    elif args.domain in DOMAINS:
        targets = [args.domain]
    else:
        parser.error("Specify a domain (" + ", ".join(DOMAINS) + "), 'all', or --base-url. "
                     "Use --list to see domains.")

    # --prefix overrides the domain default; '' explicitly disables it.
    override = None if args.prefix is None else (args.prefix or None)
    for domain in targets:
        data_file, rel_file = out_files(domain)
        prefix = override if args.prefix is not None else DOMAIN_FILTERS.get(domain)
        print(f"\n{'=' * 70}\nDomain: {domain}  ->  {data_file}"
              + (f"  ({prefix}* only)" if prefix else "") + f"\n{'=' * 70}")
        scrape_oracle_docs(domain_base_url(domain), data_file, rel_file, name_prefix=prefix)


if __name__ == "__main__":
    main()
