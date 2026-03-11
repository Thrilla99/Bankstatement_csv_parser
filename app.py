import streamlit as st
import anthropic
import base64
import json, csv, io, re, time
from datetime import datetime

# ─── PAGE CONFIG ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SA Bank → CSV",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─── CUSTOM CSS ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=Syne:wght@400;700&display=swap');
html, body, [class*="css"] { font-family: 'DM Mono', monospace; background-color: #0a0a0a; color: #d0cdc6; }
.stApp { background-color: #0a0a0a; }
h1, h2, h3 { font-family: 'Syne', sans-serif; color: #ffffff; }
.main-header {
    background: linear-gradient(135deg, #0d1a0d 0%, #0a0a0a 100%);
    border-bottom: 1px solid #1a2a1a;
    padding: 24px 32px;
    margin: -1rem -1rem 2rem -1rem;
}
.header-title { font-family: 'Syne', sans-serif; font-size: 28px; color: #ffffff; margin: 0; letter-spacing: -0.5px; }
.header-sub { font-size: 11px; color: #4a6a4a; letter-spacing: 2px; text-transform: uppercase; margin-top: 4px; }
.stat-card { background: #0d0d0d; border: 1px solid #1a2a1a; border-radius: 8px; padding: 16px; text-align: center; }
.stat-number { font-size: 28px; color: #6ab86a; font-weight: 500; }
.stat-label { font-size: 10px; color: #4a4a4a; letter-spacing: 2px; text-transform: uppercase; margin-top: 4px; }
div[data-testid="stSidebar"] { background-color: #080808; border-right: 1px solid #1a1a1a; }
.stButton > button {
    background: #0d1a0d; color: #6ab86a; border: 1px solid #1a3a1a;
    border-radius: 6px; font-family: 'DM Mono', monospace;
    letter-spacing: 0.5px; transition: all 0.2s;
}
.stButton > button:hover { background: #1a3a1a; border-color: #4a9e4a; color: #ffffff; }
.stDownloadButton > button {
    background: #0070f3 !important; color: #ffffff !important;
    border: none !important; border-radius: 6px !important;
    font-family: 'DM Mono', monospace !important; width: 100%;
}
.bank-badge {
    display: inline-block; padding: 2px 10px; border-radius: 4px;
    font-size: 11px; font-weight: 500; letter-spacing: 1px; text-transform: uppercase;
}
/* Taller drag-and-drop upload zone */
[data-testid="stFileUploader"] section {
    min-height: 180px;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 2px dashed #2a3a2a !important;
    border-radius: 10px !important;
    background: #0d0d0d !important;
    transition: border-color 0.2s;
}
[data-testid="stFileUploader"] section:hover {
    border-color: #4a9e4a !important;
}
[data-testid="stFileUploader"] section > div {
    padding: 32px 0;
}
</style>
""", unsafe_allow_html=True)

# ─── BANK PROMPTS ─────────────────────────────────────────────────────────────

PROMPTS = {

"Capitec": """You are a bank statement parser. Your ONLY output must be a valid JSON array. No explanation, no markdown, no code fences, no preamble, no postamble — just the raw JSON array starting with [ and ending with ].

TASK: Extract every transaction from this Capitec Business Account statement.

COLUMNS IN THE STATEMENT:
Post Date | Trans. Date | Description | Reference | Fees | Amount | Balance

Each object must have exactly these keys:
- "date": string DD/MM/YYYY — use Trans. Date (second date column), convert 2-digit year e.g. "01/06/25" → "01/06/2025"
- "details": string — combine Description and Reference as "Description - Reference", or just Description if no Reference
- "amount": number — the Amount column value as a signed number (negative = money out, positive = money in). Remove commas.
- "fee": number — the Fees column value as a negative number (e.g. -1.00), or 0 if the Fees column is empty for this row

AMOUNT SIGN: Amount column values already carry their sign (e.g. -521.02 is debit, +1203.69 is credit). Preserve the sign.

SPECIAL CASE — rows where Amount column is empty/zero but Fees column has a value (e.g. Monthly Service Fee, Notification Fee):
- Set "amount" to the fee value as a NEGATIVE number
- Set "fee" to 0
- Example: Monthly Service Fee with fee=-50.00, amount=empty → {"amount": -50.00, "fee": 0}

MASKED CARD ROWS (description like "******006073** **"):
- These are card/ATM transactions. Use Description as-is for details, combine with Reference: "******006073** ** - Payee Name"
- They have BOTH a fee (Fees column) AND an amount (Amount column) — output both normally

INTERNATIONAL POS rows:
- Have BOTH a fee (Fees column) AND an amount (Amount column) — output both normally

BACKDATED S/DEBIT rows:
- Have BOTH a fee (Fees column, typically -1.00) AND an amount (Amount column) — output both normally

SKIP:
- Balance brought forward line
- Interest Rate @ line
- Fee Total and VAT Total summary lines
- Any header or footer lines

Return ONLY the JSON array, nothing else.""",

"Investec": """You are a bank statement parser. Your ONLY output must be a valid JSON array. No explanation, no markdown, no code fences — just the raw JSON array starting with [ and ending with ].

TASK: Extract every transaction from the MAIN TRANSACTION TABLE of this Investec bank statement.
The main table has columns: Posted Date | Trans Date | Transaction Description | Debit | Credit | Balance

CRITICAL — the PDF has TWO sections that look like transaction tables:
1. The MAIN table (Posted Date, Trans Date, Description, Debit, Credit, Balance) — USE THIS ONE
2. A secondary "Online payments, deposits, fees and interest" summary table — IGNORE THIS ENTIRELY
3. A "Card transactions" summary table — IGNORE THIS ENTIRELY

Each object must have exactly these keys:
- "date": string DD/MM/YYYY — use Trans Date column (format "2 Feb 2026" → "02/02/2026")
- "details": string — the Transaction Description column value
- "amount": number — if Credit column has a value: POSITIVE. If Debit column has a value: NEGATIVE. Remove commas.

IMPORTANT SIGN RULES:
- Credits (money IN to the account) = POSITIVE: deposits, interest received, incoming transfers
- Debits (money OUT of the account) = NEGATIVE: payments, fees, outgoing transfers, card purchases
- "Cr Interest Adjustment" appears in the Debit column = NEGATIVE (it is interest being charged/adjusted out)
- "Credit interest" appears in the Credit column = POSITIVE

INCLUDE every row in the main table, even if the same description and amount appears multiple times on the same date (e.g. multiple "Electronic debit fee" rows on the same day are separate real transactions).

SKIP ONLY:
- "Balance brought forward" line
- "Closing Balance" line
- Any subtotal or total rows

Return ONLY the JSON array, nothing else.""",

"FNB": """You are a bank statement parser. Your ONLY output must be a valid JSON array. No explanation, no markdown, no code fences, no preamble, no postamble — just the raw JSON array starting with [ and ending with ].

TASK: Extract every transaction from this FNB Gold Business Account statement.

COLUMNS IN THE STATEMENT:
Date | Description | Amount | Balance | Accrued Bank Charges

Each object must have exactly these keys:
- "date": string DD/MM/YYYY — dates appear as "DD Mon" e.g. "01 Mar". Get the year from the statement period header line "Statement Period : DD Month YYYY to DD Month YYYY". Output as DD/MM/YYYY e.g. "01/03/2025".
- "details": string — use the full Description text. For rows prefixed with "#" (fee rows), strip the "#" e.g. "#Monthly Account Fee" → "Monthly Account Fee"
- "amount": number — if Amount ends with "Cr" it is POSITIVE (money in). If no suffix it is NEGATIVE (money out). Remove "Cr" suffix and all commas before converting. Example: "17,000.00Cr" → 17000.00, "23,600.00" → -23600.00

IGNORE completely:
- The Balance column
- The Accrued Bank Charges column — these are NOT separate transactions, do not output rows for them
- Opening Balance / Closing Balance lines
- Turnover for Statement Period section
- Any row where Amount is exactly 0.00 or missing

FEE ROWS (lines starting with "#" e.g. "#Monthly Account Fee", "#Service Fees"):
- These are normal debit rows — output ONE row each
- Amount has no "Cr" suffix so it is NEGATIVE
- Strip the "#" from details

Return ONLY the JSON array, nothing else.""",

"ABSA": """You are a bank statement parser. Extract ALL transactions from this ABSA bank statement.

Return ONLY a valid JSON array. No markdown, no code fences, no explanation.

SOURCE TABLE: Only extract from the main "Your transactions" table (columns: Date | Transaction Description | Charge | Debit Amount | Credit Amount | Balance). Pages show "Your transactions (continued)" — same table, keep extracting.

ABSA prints a small repeat summary box at the bottom of every page — it looks like 4 columns with the account number at the top and shows a few recent transactions again. IGNORE THIS ENTIRELY — it causes duplicates.

Also ignore:
- Account Summary section (Balance Brought Forward, Deposits, Sundry Credits totals)
- SERVICE FEE / MNTHLY ACCT FEE footer lines
- CHARGE legend (A = ADMINISTRATION C = CASH DEPOSIT etc.)
- Any row with no date, Bal Brought Forward

Each object must have exactly these keys:
- date (string DD/MM/YYYY — input like "3/09/2024" or "1/10/2024", normalise to DD/MM/YYYY)
- details (string — combine Transaction Description lines 1 and 2, separated by " - ". Strip spaces.)
- amount (number — Debit Amount = NEGATIVE, Credit Amount = POSITIVE. Remove commas. Ignore Charge column.)

Return ONLY the JSON array, nothing else.""",

"Nedbank": """You are a bank statement parser. Extract ALL transactions from this Nedbank bank statement.

Return ONLY a valid JSON array. No markdown, no code fences, no explanation.
Skip: BROUGHT FORWARD, CARRIED FORWARD, PROVISIONAL STATEMENT rows, any totals or summary rows.

Each object must have exactly these keys:
- date (string DD/MM/YYYY — input format is already DD/MM/YYYY)
- details (string — the Transactions column description)
- amount (number — if Debit column has value it is already negative (keep as negative), if Credit column has value it is positive. Remove commas from numbers.)

Return ONLY the JSON array, nothing else.""",

"Standard Bank": """You are a bank statement parser. Your ONLY output must be a valid JSON array. No explanation, no markdown, no code fences, no preamble, no postamble — just the raw JSON array starting with [ and ending with ].

TASK: Extract every transaction from this Standard Bank Private Banking Current Account statement.

Each object must have exactly these keys:
- "date": string DD/MM/YYYY
- "details": string
- "amount": number (negative = money out, positive = money in)

DATE RULES:
- Dates appear as "MM DD" e.g. "02 09" means February 9, "03 01" means March 1
- Find the statement year from the header line "Statement from DD Month YYYY to DD Month YYYY"
- Output format: DD/MM/YYYY e.g. "09/02/2024"
- If a transaction date falls before the statement start date, it belongs to the next year

DETAILS RULES:
- Each transaction has a main description line and sometimes a second reference line below it
- Combine both lines into one string separated by " - "
- Strip all leading/trailing whitespace

AMOUNT RULES:
- Debits: values ending with "-" e.g. "28,500.00-" → -28500.00 (NEGATIVE)
- Credits: plain values e.g. "1,500.00" → 1500.00 (POSITIVE)
- Balance column values like "177,552.74-" → IGNORE COMPLETELY (these are balances, not amounts)
- Fees marked "##" are already included in the Debits column — do NOT create extra rows for them
- Remove all commas from numbers

SKIP THESE ENTIRELY:
- BALANCE BROUGHT FORWARD line
- VAT Summary section (any rows mentioning Total VAT, VAT amount)
- Account Summary section (Balance at date of statement, etc.)
- Limit Structure section
- Any row with no date
- Any header or footer lines

Return ONLY the JSON array, nothing else.""",
}

BANK_COLORS = {
    "Capitec": "#007b5e",
    "Investec": "#003366",
    "FNB": "#cc0000",
    "ABSA": "#cc0000",
    "Nedbank": "#007b3e",
    "Standard Bank": "#0033a0",
}

BANK_LIST = ["Capitec", "Investec", "FNB", "ABSA", "Nedbank", "Standard Bank"]

# ─── BANK DETECTION ───────────────────────────────────────────────────────────

BANK_FILENAME_KEYWORDS = {
    "Capitec":       ["capitec"],
    "FNB":           ["fnb", "firstnational", "first_national"],
    "Standard Bank": ["standardbank", "standard_bank", "stanbic", "stdbank"],
    "ABSA":          ["absa"],
    "Nedbank":       ["nedbank"],
    "Investec":      ["investec"],
}

def detect_bank_from_filename(filename: str):
    """Return detected bank name from filename, or None."""
    name_lower = filename.lower().replace(" ", "_")
    for bank, keywords in BANK_FILENAME_KEYWORDS.items():
        for kw in keywords:
            if kw in name_lower:
                return bank
    return None

# ─── FUNCTIONS ────────────────────────────────────────────────────────────────

def get_client():
    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        api_key = st.session_state.get("api_key", "")
    if not api_key:
        return None
    return anthropic.Anthropic(api_key=api_key)




def is_scanned_pdf(pdf_bytes):
    """Return True if the PDF has no meaningful text layer (i.e. it is a scanned image)."""
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return len(text.strip()) < 100
    except Exception:
        return True

def pdf_to_images_b64(pdf_bytes):
    """Convert each page of a PDF to a base64-encoded PNG. Requires pymupdf (fitz)."""
    import fitz  # pymupdf
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in doc:
        mat = fitz.Matrix(2.0, 2.0)  # 2x zoom for better OCR quality
        pix = page.get_pixmap(matrix=mat)
        png_bytes = pix.tobytes("png")
        images.append(base64.standard_b64encode(png_bytes).decode("utf-8"))
    doc.close()
    return images

def extract_transactions_vision(pdf_bytes, bank, stream_status=None):
    """Send PDF pages as images to Claude vision when normal text extraction fails."""
    client = get_client()
    if not client:
        raise ValueError("No API key configured")
    prompt = PROMPTS[bank]
    images_b64 = pdf_to_images_b64(pdf_bytes)
    content_blocks = []
    for img_b64 in images_b64:
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": img_b64}
        })
    content_blocks.append({"type": "text", "text": prompt})
    raw = ""
    token_count = 0
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        messages=[{"role": "user", "content": content_blocks}]
    ) as stream:
        for text in stream.text_stream:
            raw += text
            token_count += 1
            if stream_status and token_count % 50 == 0:
                stream_status.caption(f"Receiving response (vision) — {token_count} tokens so far...")
    if stream_status:
        stream_status.caption(f"Response complete — {token_count} tokens received. Parsing...")
    return _parse_raw_json(raw)

def _parse_raw_json(raw):
    raw = raw.strip()
    raw = re.sub(r'^```json\s*', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'^```\s*', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'```\s*$', '', raw)
    start = raw.find('[')
    end = raw.rfind(']')
    if start == -1 or end == -1:
        raise ValueError("No JSON array found in Claude response")
    return json.loads(raw[start:end+1])

CHUNK_SIZE = 8  # pages per chunk for large PDFs

def split_pdf_bytes(pdf_bytes, chunk_size=CHUNK_SIZE):
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total_pages = len(doc)
    chunks = []
    for start in range(0, total_pages, chunk_size):
        end = min(start + chunk_size, total_pages)
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=start, to_page=end-1)
        buf = new_doc.tobytes()
        new_doc.close()
        chunks.append((start+1, end, buf))
    doc.close()
    return chunks

def _call_claude_stream(pdf_b64, prompt, stream_status, chunk_label=""):
    client = get_client()
    raw = ""
    token_count = 0
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": prompt}
            ]
        }]
    ) as stream:
        for text in stream.text_stream:
            raw += text
            token_count += 1
            if stream_status and token_count % 50 == 0:
                stream_status.caption(f"Receiving{chunk_label} — {token_count} tokens so far...")
    return raw, token_count

def extract_transactions(pdf_bytes, bank, stream_status=None):
    client = get_client()
    if not client:
        raise ValueError("No API key configured")
    prompt = PROMPTS[bank]

    import fitz as _fitz
    _doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
    page_count = len(_doc)
    _doc.close()

    if page_count <= CHUNK_SIZE:
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
        raw, token_count = _call_claude_stream(pdf_b64, prompt, stream_status)
        if stream_status:
            stream_status.caption(f"Response complete — {token_count} tokens. Parsing...")
        return _parse_raw_json(raw)
    else:
        chunks = split_pdf_bytes(pdf_bytes, CHUNK_SIZE)
        all_rows = []
        total_tokens = 0
        for i, (page_start, page_end, chunk_bytes) in enumerate(chunks):
            chunk_label = f" chunk {i+1}/{len(chunks)} (pages {page_start}-{page_end})"
            if stream_status:
                stream_status.caption(f"Processing{chunk_label}...")
            pdf_b64 = base64.standard_b64encode(chunk_bytes).decode("utf-8")
            raw, token_count = _call_claude_stream(pdf_b64, prompt, stream_status, chunk_label)
            total_tokens += token_count
            try:
                chunk_rows = _parse_raw_json(raw)
                all_rows.extend(chunk_rows)
            except Exception as e:
                if stream_status:
                    stream_status.caption(f"Warning: chunk {i+1} parse error — {e}")
        if stream_status:
            stream_status.caption(f"All {len(chunks)} chunks done — {total_tokens} total tokens. Merging...")
        return all_rows

def normalise_date(date_str):
    """Ensure date is in DD/MM/YYYY format. Handles common variants."""
    if not date_str:
        return date_str
    date_str = date_str.strip()
    # Already correct format DD/MM/YYYY
    if re.match(r'^\d{2}/\d{2}/\d{4}$', date_str):
        return date_str
    # D/MM/YYYY or D/M/YYYY -> pad to DD/MM/YYYY
    if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', date_str):
        parts = date_str.split('/')
        return f"{int(parts[0]):02d}/{int(parts[1]):02d}/{parts[2]}"
    # DD/MM/YY -> add 20 prefix for year
    if re.match(r'^\d{2}/\d{2}/\d{2}$', date_str):
        parts = date_str.split('/')
        return f"{parts[0]}/{parts[1]}/20{parts[2]}"
    return date_str

def build_rows(raw, bank):
    """Normalise extracted rows into standard {date, details, amount} format.
    For Capitec only: explode fee rows into separate Service Fee entries."""
    result = []
    for r in raw:
        date = normalise_date(r.get('date', ''))
        details = r.get('details', '')
        amount = float(r.get('amount', 0) or 0)

        if bank == "Capitec":
            fee = float(r.get('fee', 0) or 0)
            # Always add the main transaction row
            result.append({'date': date, 'details': details, 'amount': amount})
            # If there's also a fee on this row, add a separate Service Fee row
            if fee != 0:
                result.append({'date': date, 'details': 'Service Fee', 'amount': fee})
        else:
            result.append({'date': date, 'details': details, 'amount': amount})
    return result


def deduplicate_rows(rows):
    # If over 35% of rows are dupes, the PDF was doubled - remove all dupes
    if not rows:
        return rows
    seen = []
    deduped = []
    for r in rows:
        key = (r.get("date",""), r.get("details",""), str(r.get("amount","")))
        if key not in seen:
            seen.append(key)
            deduped.append(r)
    dupe_ratio = 1 - len(deduped) / max(len(rows), 1)
    if dupe_ratio > 0.35:
        return deduped
    # Otherwise only strip consecutive dupes (safer for legitimately repeated txns)
    result = [rows[0]]
    for r in rows[1:]:
        prev = result[-1]
        if (r.get("date") == prev.get("date") and
                r.get("details") == prev.get("details") and
                str(r.get("amount")) == str(prev.get("amount"))):
            continue
        result.append(r)
    return result

def rows_to_csv_bytes(rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Details', 'Amount'])
    for row in rows:
        writer.writerow([row['date'], row['details'], row['amount']])
    return output.getvalue().encode('utf-8')

def get_month_key(date_str):
    if not date_str:
        return 'Unknown'
    parts = date_str.split('/')
    if len(parts) < 3:
        return 'Unknown'
    months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    try:
        m = int(parts[1]) - 1
        y = parts[2]
        return f"{months[m]}_{y}"
    except:
        return 'Unknown'

# ─── SESSION STATE ────────────────────────────────────────────────────────────
if 'processed_files' not in st.session_state:
    st.session_state.processed_files = []
if 'all_rows' not in st.session_state:
    st.session_state.all_rows = []
if 'confirmed_bank' not in st.session_state:
    st.session_state.confirmed_bank = None
if 'confirmed_files' not in st.session_state:
    st.session_state.confirmed_files = []
if 'confirm_pending' not in st.session_state:
    st.session_state.confirm_pending = False
if 'history' not in st.session_state:
    st.session_state.history = []
if 'cached_upload_bytes' not in st.session_state:
    st.session_state.cached_upload_bytes = {}

# ─── SIDEBAR ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### SA Bank → CSV")
    st.markdown("---")

    st.markdown("**Anthropic API Key**")
    api_key_input = st.text_input(
        "API Key", type="password",
        placeholder="Paste your Anthropic API key",
        label_visibility="collapsed", key="api_key"
    )
    st.caption("Get a key from [console.anthropic.com](https://console.anthropic.com)")
    st.markdown("---")

    st.markdown("**Select Bank**")
    selected_bank = st.selectbox(
        "Bank", BANK_LIST,
        label_visibility="collapsed", key="selected_bank"
    )
    st.markdown("---")

    st.markdown("**Output format**")
    st.caption("Date · Details · Amount")
    st.caption("Signed Amount: positive = money in, negative = money out")
    st.markdown("---")

    if selected_bank == "Capitec":
        st.markdown("**Capitec fee rows**")
        st.caption("Fees are automatically split into separate **Service Fee** rows.")
        st.markdown("---")

    st.markdown("**Pastel tip**")
    st.caption("Date + Details + Amount maps directly into Pastel's import format.")
    st.markdown("---")



# ─── HEADER ───────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="main-header">
    <div class="header-title">SA Bank Statement → CSV</div>
    <div class="header-sub">Multi-Bank Extractor · Capitec · Investec · FNB · ABSA · Nedbank · Standard Bank · Powered by Claude AI</div>
</div>
""", unsafe_allow_html=True)

# ─── STATS ────────────────────────────────────────────────────────────────────
if st.session_state.all_rows:
    col1, col2, col3, col4 = st.columns(4)
    total = len(st.session_state.all_rows)
    fee_count = sum(1 for r in st.session_state.all_rows if r['details'] == 'Service Fee')
    txn_count = total - fee_count
    files_done = len(st.session_state.processed_files)
    with col1:
        st.markdown(f'<div class="stat-card"><div class="stat-number">{files_done}</div><div class="stat-label">Files Processed</div></div>', unsafe_allow_html=True)
    with col2:
        st.markdown(f'<div class="stat-card"><div class="stat-number">{txn_count}</div><div class="stat-label">Transactions</div></div>', unsafe_allow_html=True)
    with col3:
        st.markdown(f'<div class="stat-card"><div class="stat-number">{fee_count}</div><div class="stat-label">Fee Rows</div></div>', unsafe_allow_html=True)
    with col4:
        st.markdown(f'<div class="stat-card"><div class="stat-number">{total}</div><div class="stat-label">Total Rows</div></div>', unsafe_allow_html=True)
    st.markdown("")

# ─── UPLOAD ───────────────────────────────────────────────────────────────────
st.markdown(f"#### Upload {selected_bank} Statements")
uploaded_files = st.file_uploader(
    f"Drop {selected_bank} PDF bank statements here",
    type=["pdf"],
    accept_multiple_files=True,
    label_visibility="collapsed"
)

# Cache bytes immediately so bank-switch rerun doesn't lose files
if uploaded_files:
    for uf in uploaded_files:
        if uf.name not in st.session_state.cached_upload_bytes:
            st.session_state.cached_upload_bytes[uf.name] = uf.read()
    current_names = {uf.name for uf in uploaded_files}
    st.session_state.cached_upload_bytes = {
        k: v for k, v in st.session_state.cached_upload_bytes.items()
        if k in current_names
    }

# ── Step 2: Extraction (runs on rerun AFTER confirm — uploader will be empty) ──
if st.session_state.confirmed_bank and st.session_state.confirmed_files:
    confirmed_bank = st.session_state.confirmed_bank
    files_to_process = st.session_state.confirmed_files

    total_files = len(files_to_process)
    # Estimate ~25s per file for text PDFs, ~45s for vision (scanned)
    est_seconds = total_files * 25
    est_str = f"{est_seconds // 60}m {est_seconds % 60}s" if est_seconds >= 60 else f"~{est_seconds}s"

    st.markdown(f"#### Extracting {total_files} file{'s' if total_files > 1 else ''} as {confirmed_bank}")
    progress = st.progress(0)
    status = st.empty()
    stream_status = st.empty()
    timing = st.empty()
    start_all = time.time()

    for i, file_data in enumerate(files_to_process):
        file_start = time.time()
        files_left = total_files - i
        elapsed = time.time() - start_all
        avg_per_file = (elapsed / i) if i > 0 else 25
        est_remaining = int(avg_per_file * files_left)
        est_rem_str = f"{est_remaining // 60}m {est_remaining % 60}s" if est_remaining >= 60 else f"{est_remaining}s"
        eta_label = f"Est. remaining: {est_rem_str}" if i > 0 else f"Est. total: {est_str}"

        status.markdown(f"Processing **{file_data['name']}** ({i+1}/{total_files})")
        stream_status.caption("Sending PDF to Claude...")
        timing.caption(eta_label)

        try:
            scanned = is_scanned_pdf(file_data['bytes'])
            if scanned:
                status.markdown(f"Processing **{file_data['name']}** ({i+1}/{total_files}) — scanned PDF, using vision...")
                stream_status.caption("Converting pages to images...")
                timing.caption(f"{eta_label}  |  Vision mode (~45s per file)")
                try:
                    raw = extract_transactions_vision(file_data['bytes'], confirmed_bank, stream_status=stream_status)
                    vision_used = True
                except Exception as ve:
                    raise ValueError(f"VISION_FAILED: {ve}")
            else:
                raw = extract_transactions(file_data['bytes'], confirmed_bank, stream_status=stream_status)
                vision_used = False

            rows = build_rows(raw, confirmed_bank)
            fee_rows = sum(1 for r in rows if r['details'] == 'Service Fee')
            txn_rows = len(rows) - fee_rows
            elapsed_file = int(time.time() - file_start)

            stream_status.caption(f"Done — {txn_rows} transactions extracted in {elapsed_file}s")

            st.session_state.processed_files.append({
                'name': file_data['name'],
                'bank': confirmed_bank,
                'rows': rows,
                'txn_count': txn_rows,
                'fee_count': fee_rows,
                'status': 'done',
                'vision': vision_used,
                'elapsed': elapsed_file
            })
            st.session_state.all_rows.extend(rows)

        except Exception as e:
            error_msg = str(e)
            stream_status.empty()
            if error_msg.startswith("VISION_FAILED"):
                detail = error_msg.replace("VISION_FAILED: ", "")
                st.error(
                    f"**{file_data['name']}** — vision extraction also failed. "
                    f"The scan quality may be too low to read.\n\nDetail: {detail}"
                )
            st.session_state.processed_files.append({
                'name': file_data['name'],
                'bank': confirmed_bank,
                'rows': [],
                'status': 'error',
                'error': error_msg
            })

        progress.progress((i + 1) / total_files)

    total_elapsed = int(time.time() - start_all)
    elapsed_str = f"{total_elapsed // 60}m {total_elapsed % 60}s" if total_elapsed >= 60 else f"{total_elapsed}s"
    timing.caption(f"Done in {elapsed_str}")

    # Save completed session to history (keep last 3)
    done_files = [f for f in st.session_state.processed_files if f['status'] == 'done']
    if done_files:
        import copy
        from datetime import datetime as _dt
        history_entry = {
            'timestamp': _dt.now().strftime("%d %b %Y, %H:%M"),
            'bank': confirmed_bank,
            'files': copy.deepcopy(done_files)
        }
        st.session_state.history.insert(0, history_entry)
        st.session_state.history = st.session_state.history[:3]

    st.session_state.confirmed_bank = None
    st.session_state.confirmed_files = []
    st.session_state.cached_upload_bytes = {}
    status.empty()
    stream_status.empty()
    progress.empty()
    st.rerun()

# ── Step 1: Show confirmation panel when files are uploaded ────────────────────
elif uploaded_files:
    already_processed = {f['name'] for f in st.session_state.processed_files}
    new_files = [f for f in uploaded_files if f.name not in already_processed]

    if new_files:
        st.markdown("---")
        st.markdown("#### Confirm before extracting")

        # Check each file for bank mismatch
        file_rows = []
        any_mismatch = False
        for f in new_files:
            detected = detect_bank_from_filename(f.name)
            if detected and detected != selected_bank:
                status_icon = "[!]"
                note = f"Filename suggests **{detected}** — you have **{selected_bank}** selected"
                any_mismatch = True
            else:
                status_icon = "[ok]"
                note = f"Will be processed as **{selected_bank}**"
            file_rows.append((status_icon, f.name, note))

        # Show file list
        for icon, fname, note in file_rows:
            st.markdown(
                f"""<div style="background:#0d0d0d; border:1px solid #1a2a1a; border-radius:6px;
                padding:10px 14px; margin-bottom:6px; display:flex; gap:12px; align-items:center;">
                <span style="font-size:18px">{icon}</span>
                <div>
                    <div style="color:#ffffff; font-size:13px">{fname}</div>
                    <div style="color:#4a6a4a; font-size:11px; margin-top:2px">{note}</div>
                </div></div>""",
                unsafe_allow_html=True
            )

        if any_mismatch:
            st.warning(
                "One or more files may not match the selected bank. "
                "Processing with the wrong prompt wastes API tokens and gives bad results. "
                "Switch the bank in the sidebar, or confirm below to proceed anyway."
            )

        st.markdown("")
        col_confirm, col_cancel = st.columns(2)

        with col_confirm:
            if st.button("Confirm — process files as " + selected_bank, use_container_width=True):
                # Read all bytes NOW before rerun clears the uploader
                st.session_state.confirmed_bank = selected_bank
                st.session_state.confirmed_files = [
                    {'name': f.name, 'bytes': st.session_state.cached_upload_bytes.get(f.name, b'')}
                    for f in new_files
                ]
                st.rerun()

        with col_cancel:
            if st.button("✗ Cancel", use_container_width=True):
                st.session_state.confirmed_bank = None
                st.session_state.confirmed_files = []
                st.rerun()

# ─── TABS ─────────────────────────────────────────────────────────────────────
tab_results, tab_history = st.tabs(["Results", "History"])

with tab_results:
    if st.session_state.processed_files:
        col_hdr, col_clr = st.columns([4, 1])
        with col_hdr:
            st.markdown("#### Processed Files")
        with col_clr:
            if st.button("Clear files", use_container_width=True):
                st.session_state.processed_files = []
                st.session_state.all_rows = []
                st.rerun()

        for idx, f in enumerate(st.session_state.processed_files):
            col_a, col_b = st.columns([3, 1])
            with col_a:
                bank_label = f.get('bank', '')
                if f['status'] == 'done':
                    fee_info = f" + {f['fee_count']} fee rows" if f['fee_count'] > 0 else ""
                    vision_tag = " [vision]" if f.get("vision") else ""
                    elapsed_tag = f" — {f['elapsed']}s" if f.get("elapsed") else ""
                    st.success(f"**{f['name']}** [{bank_label}]{vision_tag} — {f['txn_count']} transactions{fee_info} = {len(f['rows'])} total{elapsed_tag}")
                else:
                    st.error(f"**{f['name']}** [{bank_label}] — {f.get('error', 'Unknown error')}")
            with col_b:
                if f['status'] == 'done':
                    csv_bytes = rows_to_csv_bytes(f['rows'])
                    st.download_button(
                        "Download CSV",
                        data=csv_bytes,
                        file_name=f['name'].replace('.pdf', '.csv'),
                        mime='text/csv',
                        key=f"dl_{idx}_{f['name']}"
                    )

        if st.session_state.all_rows:
            st.markdown("---")
            st.markdown("#### Download")
            col1, col2 = st.columns(2)
            with col1:
                all_csv = rows_to_csv_bytes(st.session_state.all_rows)
                st.download_button(
                    "Download All Combined",
                    data=all_csv,
                    file_name="sa_bank_all_transactions.csv",
                    mime='text/csv',
                    use_container_width=True
                )
            with col2:
                by_month = {}
                for row in st.session_state.all_rows:
                    m = get_month_key(row['date'])
                    by_month.setdefault(m, []).append(row)
                month_options = sorted(by_month.keys())
                selected_month = st.selectbox("Download specific month:", ['All months'] + month_options)
                if selected_month != 'All months':
                    month_csv = rows_to_csv_bytes(by_month[selected_month])
                    st.download_button(
                        f"Download {selected_month}",
                        data=month_csv,
                        file_name=f"sa_bank_{selected_month}.csv",
                        mime='text/csv',
                        use_container_width=True
                    )

            st.markdown("---")
            st.markdown("#### Preview")
            preview_rows = st.session_state.all_rows[:50]
            table_data = []
            for r in preview_rows:
                amt = r['amount']
                table_data.append({
                    'Date': r['date'],
                    'Details': r['details'],
                    'Amount': f"+{amt}" if isinstance(amt, (int, float)) and amt > 0 else str(amt)
                })
            if table_data:
                st.dataframe(table_data, use_container_width=True, height=400)
                if len(st.session_state.all_rows) > 50:
                    st.caption(f"Showing first 50 of {len(st.session_state.all_rows)} rows")

    elif not uploaded_files and not st.session_state.confirmed_files:
        banks_str = " · ".join(BANK_LIST)
        st.markdown(
            f'<div style="text-align:center; padding: 60px 40px; color: #2a2a2a; border: 2px dashed #1a1a1a; border-radius: 12px; margin-top: 20px;">'
            f'<div style="font-size: 16px; color: #444; margin-bottom: 8px; margin-top: 8px;">Select your bank in the sidebar, then upload PDF statements</div>'
            f'<div style="font-size: 12px; color: #333;">{banks_str}</div>'
            f'<div style="font-size: 12px; margin-top: 8px;">Output: Date · Details · Amount (signed) · Pastel-ready</div>'
            f'</div>',
            unsafe_allow_html=True
        )

with tab_history:
    if not st.session_state.history:
        st.markdown(
            '<div style="text-align:center; padding: 60px 40px; color: #2a2a2a; border: 2px dashed #1a1a1a; border-radius: 12px; margin-top: 20px;">'
            '<div style="font-size: 16px; color: #444; margin-bottom: 8px; margin-top: 8px;">No history yet — completed sessions will appear here</div>'
            '<div style="font-size: 12px; color: #333;">Last 3 sessions are saved automatically</div>'
            '</div>',
            unsafe_allow_html=True
        )
    else:
        for hi, entry in enumerate(st.session_state.history):
            st.markdown(f"**{entry['timestamp']}** — {entry['bank']} — {len(entry['files'])} file{'s' if len(entry['files']) > 1 else ''}")
            for fi, f in enumerate(entry['files']):
                fee_info = f" + {f.get('fee_count', 0)} fee rows" if f.get('fee_count', 0) > 0 else ""
                col_a, col_b = st.columns([3, 1])
                with col_a:
                    st.markdown(f"&nbsp;&nbsp;&nbsp;{f['name']} — {f['txn_count']} transactions{fee_info}")
                with col_b:
                    hist_csv = rows_to_csv_bytes(f['rows'])
                    st.download_button(
                        "Download CSV",
                        data=hist_csv,
                        file_name=f['name'].replace('.pdf', '.csv'),
                        mime='text/csv',
                        key=f"hist_{hi}_{fi}_{f['name']}"
                    )
            if len(entry['files']) > 1:
                all_session_rows = []
                for f in entry['files']:
                    all_session_rows.extend(f['rows'])
                session_csv = rows_to_csv_bytes(all_session_rows)
                ts_safe = entry['timestamp'].replace(', ', '_').replace(' ', '_').replace(':', '')
                st.download_button(
                    "Download all from this session",
                    data=session_csv,
                    file_name=f"session_{ts_safe}.csv",
                    mime='text/csv',
                    key=f"hist_all_{hi}"
                )
            st.markdown("---")
