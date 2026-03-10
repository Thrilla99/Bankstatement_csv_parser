import streamlit as st
import google.generativeai as genai
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

"Capitec": """You are a bank statement parser. Extract ALL transactions from this Capitec bank statement.

Return ONLY a valid JSON array. No markdown, no code fences, no explanation.
Skip: Balance brought forward, Interest Rate, Fee Total, VAT Total lines.

Each object must have exactly these keys:
- date (string DD/MM/YYYY — use post date, convert 2-digit year to 4-digit e.g. 01/05/2025)
- details (string — combine Description and Reference as "Description - Reference", or just Description if no reference)
- amount (number — positive=money in, negative=money out; if amount is 0 but fees column has value, use the fee value as negative)
- fee (number — fee value as negative number, or 0 if no fee)
- is_fee_only (boolean — true if the original row only had a fee value and no transaction amount)

Rules:
- For rows where amount=0 but fee exists (e.g. Monthly Service Fee, Notification Fee): set amount=fee as negative, fee=0, is_fee_only=true
- For normal rows with both amount and fee: keep amount as-is, set fee as negative number

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

"FNB": """You are a bank statement parser. Extract ALL transactions from this FNB (First National Bank) bank statement.

Return ONLY a valid JSON array. No markdown, no code fences, no explanation.
Skip: Opening Balance, Closing Balance, any totals or summary rows.

Each object must have exactly these keys:
- date (string DD/MM/YYYY — input format is like "02 Jan", reconstruct the full year from the statement period header at the top of the statement)
- details (string — combine Description line 1 and Description line 2 if present, separated by " - ". Strip leading/trailing spaces.)
- amount (number — if Amount column ends with "Cr" it is positive (money in), if no suffix or ends with "Dr" it is negative (money out). Strip the Cr/Dr suffix before converting to number. Remove commas from numbers.)

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

"Standard Bank": """You are a bank statement parser. Extract ALL transactions from this Standard Bank bank statement.

Return ONLY a valid JSON array. No markdown, no code fences, no explanation.
Skip: BALANCE BROUGHT FORWARD, VAT Summary rows, Account Summary rows, any totals rows.

Each object must have exactly these keys:
- date (string DD/MM/YYYY — input format is "MM DD" like "05 13" meaning May 13. Reconstruct the full year from the statement period header. Output as DD/MM/YYYY.)
- details (string — combine Details line 1 and line 2 if present, separated by " - ". Strip leading/trailing spaces.)
- amount (number — if Debits column has value ending in "-" (e.g. "5,000.00-") it is negative (money out), strip the "-" suffix. If Credits column has value it is positive. Remove commas from numbers.)

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

# ─── FUNCTIONS ────────────────────────────────────────────────────────────────

def get_model():
    api_key = st.session_state.get("api_key", "")
    if not api_key:
        return None
    genai.configure(api_key=api_key)
    return genai.GenerativeModel("gemini-2.0-flash")

def extract_transactions(pdf_bytes, bank):
    model = get_model()
    if not model:
        raise ValueError("No API key configured")
    prompt = PROMPTS[bank]
    response = model.generate_content([
        {"mime_type": "application/pdf", "data": pdf_bytes},
        prompt
    ])
    raw = response.text.strip()
    raw = re.sub(r'^```json\s*', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'^```\s*', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'```\s*$', '', raw)
    start = raw.find('[')
    end = raw.rfind(']')
    if start == -1 or end == -1:
        raise ValueError("No JSON array found in Gemini response")
    return json.loads(raw[start:end+1])

def build_rows(raw, bank):
    """Normalise extracted rows into standard {date, details, amount} format.
    For Capitec only: explode fee rows."""
    result = []
    for r in raw:
        date = r.get('date', '')
        details = r.get('details', '')
        amount = float(r.get('amount', 0) or 0)

        if bank == "Capitec":
            fee = float(r.get('fee', 0) or 0)
            is_fee_only = r.get('is_fee_only', False)
            if is_fee_only:
                result.append({'date': date, 'details': details, 'amount': amount})
            else:
                result.append({'date': date, 'details': details, 'amount': amount})
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

# ─── SIDEBAR ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⬡ SA Bank → CSV")
    st.markdown("---")

    st.markdown("**🔑 Gemini API Key**")
    api_key = st.text_input(
        "API Key", type="password",
        placeholder="Paste your Gemini API key",
        label_visibility="collapsed", key="api_key"
    )
    st.caption("Free key from [aistudio.google.com](https://aistudio.google.com)")
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
    <div class="header-sub">Multi-Bank Extractor · Capitec · Investec · FNB · ABSA · Nedbank · Standard Bank · Powered by Gemini AI</div>
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

if uploaded_files:
    already_processed = {f['name'] for f in st.session_state.processed_files}
    new_files = [f for f in uploaded_files if f.name not in already_processed]

    if new_files:
        if not api_key:
            st.warning("⚠️ Please enter your Gemini API key in the sidebar first.")
        else:
            btn_label = f"▶ Extract {len(new_files)} {selected_bank} file{'s' if len(new_files) > 1 else ''} with Gemini AI"
            if st.button(btn_label, use_container_width=True):
                progress = st.progress(0)
                status = st.empty()

                for i, uploaded_file in enumerate(new_files):
                    status.markdown(f"🤖 Processing **{uploaded_file.name}** ({selected_bank})...")
                    try:
                        pdf_bytes = uploaded_file.read()
                        raw = extract_transactions(pdf_bytes, selected_bank)
                        rows = build_rows(raw, selected_bank)
                        fee_rows = sum(1 for r in rows if r['details'] == 'Service Fee')
                        txn_rows = len(rows) - fee_rows

                        st.session_state.processed_files.append({
                            'name': uploaded_file.name,
                            'bank': selected_bank,
                            'rows': rows,
                            'txn_count': txn_rows,
                            'fee_count': fee_rows,
                            'status': 'done'
                        })
                        st.session_state.all_rows.extend(rows)
                        progress.progress((i + 1) / len(new_files))

                    except Exception as e:
                        st.session_state.processed_files.append({
                            'name': uploaded_file.name,
                            'bank': selected_bank,
                            'rows': [],
                            'status': 'error',
                            'error': str(e)
                        })
                        progress.progress((i + 1) / len(new_files))

                status.empty()
                progress.empty()
                st.rerun()

# ─── PROCESSED FILES ─────────────────────────────────────────────────────────
if st.session_state.processed_files:
    st.markdown("#### 📂 Processed Files")
    for f in st.session_state.processed_files:
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
                    key=f"dl_{f['name']}"
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
