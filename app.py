import streamlit as st
import anthropic
import base64
import json, csv, io, re
from datetime import datetime

# ─── PAGE CONFIG ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SA Bank → CSV",
    page_icon="⬡",
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

SKIP:
- Balance brought forward line
- Interest Rate @ line
- Fee Total and VAT Total summary lines
- Any header or footer lines

Return ONLY the JSON array, nothing else.""",

"Investec": """You are a bank statement parser. Extract ALL transactions from this Investec bank statement.

Return ONLY a valid JSON array. No markdown, no code fences, no explanation.
Skip: Balance brought forward, Closing Balance, any summary or totals rows.
Only include rows from the main transaction table (pages 1-2 typically). Ignore secondary summary tables.

Each object must have exactly these keys:
- date (string DD/MM/YYYY — input format is like "2 Feb 2026", convert to DD/MM/YYYY)
- details (string — the Transaction Description column)
- amount (number — positive if Credit column has value, negative if Debit column has value)

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
Skip: Bal Brought Forward, any totals or summary rows.
IGNORE the Charge column entirely — do not create any rows from it.

Each object must have exactly these keys:
- date (string DD/MM/YYYY — input format is like "3/09/2024", normalise to DD/MM/YYYY)
- details (string — combine Transaction Description line 1 and line 2 if present, separated by " - ". Strip leading/trailing spaces.)
- amount (number — if Debit Amount column has value it is negative (money out), if Credit Amount column has value it is positive (money in). Remove commas from numbers.)

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

def extract_transactions(pdf_bytes, bank):
    client = get_client()
    if not client:
        raise ValueError("No API key configured")
    prompt = PROMPTS[bank]
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,  # Increased for long statements (e.g. 8-page Standard Bank)
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64
                    }
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }]
    )
    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    raw = re.sub(r'^```json\s*', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'^```\s*', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'```\s*$', '', raw)
    start = raw.find('[')
    end = raw.rfind(']')
    if start == -1 or end == -1:
        raise ValueError("No JSON array found in Claude response")
    return json.loads(raw[start:end+1])

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
    st.session_state.confirm_pending = False  # True when awaiting user confirmation

# ─── SIDEBAR ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⬡ SA Bank → CSV")
    st.markdown("---")

    st.markdown("**🔑 Anthropic API Key**")
    api_key_input = st.text_input(
        "API Key", type="password",
        placeholder="Paste your Anthropic API key",
        label_visibility="collapsed", key="api_key"
    )
    st.caption("Get a key from [console.anthropic.com](https://console.anthropic.com)")
    st.markdown("---")

    st.markdown("**🏦 Select Bank**")
    selected_bank = st.selectbox(
        "Bank", BANK_LIST,
        label_visibility="collapsed", key="selected_bank"
    )
    st.markdown("---")

    st.markdown("**📋 Output format**")
    st.caption("Date · Details · Amount")
    st.caption("Signed Amount: positive = money in, negative = money out")
    st.markdown("---")

    if selected_bank == "Capitec":
        st.markdown("**ℹ️ Capitec fee rows**")
        st.caption("Fees are automatically split into separate **Service Fee** rows.")
        st.markdown("---")

    st.markdown("**💡 Pastel tip**")
    st.caption("Date + Details + Amount maps directly into Pastel's import format.")

# ─── HEADER ───────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="main-header">
    <div class="header-title">⬡ SA Bank Statement → CSV</div>
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
st.markdown(f"#### 📄 Upload {selected_bank} Statements")
uploaded_files = st.file_uploader(
    f"Drop {selected_bank} PDF bank statements here",
    type=["pdf"],
    accept_multiple_files=True,
    label_visibility="collapsed"
)

# ── Step 2: Extraction (runs on rerun AFTER confirm — uploader will be empty) ──
if st.session_state.confirmed_bank and st.session_state.confirmed_files:
    confirmed_bank = st.session_state.confirmed_bank
    files_to_process = st.session_state.confirmed_files

    st.markdown(f"#### 🤖 Extracting {len(files_to_process)} file{'s' if len(files_to_process) > 1 else ''} as {confirmed_bank}...")
    progress = st.progress(0)
    status = st.empty()

    for i, file_data in enumerate(files_to_process):
        status.markdown(f"Processing **{file_data['name']}** ({i+1}/{len(files_to_process)})...")
        try:
            raw = extract_transactions(file_data['bytes'], confirmed_bank)
            rows = build_rows(raw, confirmed_bank)
            fee_rows = sum(1 for r in rows if r['details'] == 'Service Fee')
            txn_rows = len(rows) - fee_rows

            st.session_state.processed_files.append({
                'name': file_data['name'],
                'bank': confirmed_bank,
                'rows': rows,
                'txn_count': txn_rows,
                'fee_count': fee_rows,
                'status': 'done'
            })
            st.session_state.all_rows.extend(rows)

        except Exception as e:
            st.session_state.processed_files.append({
                'name': file_data['name'],
                'bank': confirmed_bank,
                'rows': [],
                'status': 'error',
                'error': str(e)
            })

        progress.progress((i + 1) / len(files_to_process))

    # Clear confirmation state and rerun to show results
    st.session_state.confirmed_bank = None
    st.session_state.confirmed_files = []
    status.empty()
    progress.empty()
    st.rerun()

# ── Step 1: Show confirmation panel when files are uploaded ────────────────────
elif uploaded_files:
    already_processed = {f['name'] for f in st.session_state.processed_files}
    new_files = [f for f in uploaded_files if f.name not in already_processed]

    if new_files:
        st.markdown("---")
        st.markdown("#### ✅ Confirm before extracting")

        # Check each file for bank mismatch
        file_rows = []
        any_mismatch = False
        for f in new_files:
            detected = detect_bank_from_filename(f.name)
            if detected and detected != selected_bank:
                status_icon = "⚠️"
                note = f"Filename suggests **{detected}** — you have **{selected_bank}** selected"
                any_mismatch = True
            else:
                status_icon = "✅"
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
                "⚠️ One or more files may not match the selected bank. "
                "Processing with the wrong prompt wastes API tokens and gives bad results. "
                "Switch the bank in the sidebar, or confirm below to proceed anyway."
            )

        st.markdown("")
        col_confirm, col_cancel = st.columns(2)

        with col_confirm:
            if st.button(f"✅ Yes, process {len(new_files)} file{'s' if len(new_files) > 1 else ''} as {selected_bank}", use_container_width=True):
                # Read all bytes NOW before rerun clears the uploader
                st.session_state.confirmed_bank = selected_bank
                st.session_state.confirmed_files = [
                    {'name': f.name, 'bytes': f.read()} for f in new_files
                ]
                st.rerun()

        with col_cancel:
            if st.button("✗ Cancel", use_container_width=True):
                st.session_state.confirmed_bank = None
                st.session_state.confirmed_files = []
                st.rerun()

# ─── PROCESSED FILES ─────────────────────────────────────────────────────────
if st.session_state.processed_files:
    st.markdown("####  Processed Files")
    for idx, f in enumerate(st.session_state.processed_files):
        col_a, col_b = st.columns([3, 1])
        with col_a:
            bank_label = f.get('bank', '')
            if f['status'] == 'done':
                fee_info = f" + {f['fee_count']} fee rows" if f['fee_count'] > 0 else ""
                st.success(f"✅ **{f['name']}** [{bank_label}] — {f['txn_count']} transactions{fee_info} = {len(f['rows'])} total")
            else:
                st.error(f"❌ **{f['name']}** [{bank_label}] — {f.get('error', 'Unknown error')}")
        with col_b:
            if f['status'] == 'done':
                csv_bytes = rows_to_csv_bytes(f['rows'])
                st.download_button(
                    "⬇ CSV",
                    data=csv_bytes,
                    file_name=f['name'].replace('.pdf', '.csv'),
                    mime='text/csv',
                    key=f"dl_{idx}_{f['name']}"
                )

    # ─── DOWNLOADS ───────────────────────────────────────────────────────────
    if st.session_state.all_rows:
        st.markdown("---")
        st.markdown("#### ⬇ Download")
        col1, col2 = st.columns(2)

        with col1:
            all_csv = rows_to_csv_bytes(st.session_state.all_rows)
            st.download_button(
                "⬇ Download All Combined",
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
                    f"⬇ Download {selected_month}",
                    data=month_csv,
                    file_name=f"sa_bank_{selected_month}.csv",
                    mime='text/csv',
                    use_container_width=True
                )

        # ─── PREVIEW ─────────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown("#### 👁 Preview")
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

    st.markdown("---")
    if st.button("🗑 Clear all and start over"):
        st.session_state.processed_files = []
        st.session_state.all_rows = []
        st.rerun()

elif not uploaded_files:
    banks_str = " · ".join(BANK_LIST)
    st.markdown(f"""
    <div style="text-align:center; padding: 60px 40px; color: #2a2a2a; border: 2px dashed #1a1a1a; border-radius: 12px; margin-top: 20px;">
        <div style="font-size: 48px; margin-bottom: 16px;">🏦</div>
        <div style="font-size: 16px; color: #444; margin-bottom: 8px;">Select your bank in the sidebar, then upload PDF statements</div>
        <div style="font-size: 12px; color: #333;">{banks_str}</div>
        <div style="font-size: 12px; margin-top: 8px;">Output: Date · Details · Amount (signed) · Pastel-ready</div>
    </div>
    """, unsafe_allow_html=True)
