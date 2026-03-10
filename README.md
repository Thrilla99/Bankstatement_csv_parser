# SA Bank Statement → CSV Converter

Convert South African bank statement PDFs into clean CSVs for Pastel accounting.

## Supported Banks
- Capitec
- Investec
- FNB (First National Bank)
- ABSA
- Nedbank
- Standard Bank

## Output Format
`Date, Details, Amount` — signed single Amount column (positive = money in, negative = money out). Pastel-ready.

## Setup
1. Get a free Gemini API key from [aistudio.google.com](https://aistudio.google.com)
2. Paste the key into the sidebar when the app opens
3. Select your bank, upload PDFs, click Extract

## Run Locally
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud
See deployment guide in the project docs.
