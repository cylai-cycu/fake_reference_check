# app.py (One-Click Citation Report Automation - Local Enhanced Version)

import streamlit as st
import pandas as pd
import time
import os
import re
import ast
import difflib
import subprocess
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==============================================================================
# 1. 自動安裝與設定 AnyStyle (針對雲端環境修正)
# ==============================================================================
def install_and_setup_anystyle():
    # 避免在 Streamlit 重跑 script 時重複執行輸出，雖非必須但可讓 log 乾淨點
    if "anystyle_setup_done" in st.session_state:
        return

    print("🔄 開始檢查 AnyStyle 環境...")
    
    # (A) 嘗試安裝 (如果尚未安裝)
    if shutil.which("anystyle") is None:
        print("⚠️ 尚未偵測到 anystyle，嘗試透過 gem 安裝...")
        try:
            # 先嘗試全域安裝
            subprocess.run(["gem", "install", "anystyle-cli"], check=True)
            print("✅ Gem 全域安裝成功")
        except subprocess.CalledProcessError:
            try:
                # 若失敗則嘗試 user install
                subprocess.run(["gem", "install", "--user-install", "anystyle-cli"], check=True)
                print("✅ Gem 使用者安裝成功")
            except Exception as e:
                print(f"❌ 安裝失敗: {e}")

    # (B) 強制將 Gem 的 bin 目錄加入 PATH
    # 這是最關鍵的一步，解決找不到指令的問題
    try:
        result = subprocess.run(["gem", "environment", "bin"], capture_output=True, text=True)
        if result.returncode == 0:
            gem_bin_path = result.stdout.strip()
            current_path = os.environ.get("PATH", "")
            
            if gem_bin_path not in current_path:
                print(f"🔧 將 Gem 路徑加入系統 PATH: {gem_bin_path}")
                os.environ["PATH"] += os.pathsep + gem_bin_path
            
    except Exception as e:
        print(f"❌ 路徑設定發生錯誤: {e}")
        
    st.session_state["anystyle_setup_done"] = True

# 執行安裝檢查
install_and_setup_anystyle()

# ==============================================================================
# 2. 匯入自定義模組 (包含錯誤處理)
# ==============================================================================
try:
    from modules.parsers import parse_references_with_anystyle
    from modules.local_db import load_csv_data, search_local_database
    from modules.api_clients import (
        get_scopus_key,
        get_serpapi_key,
        search_crossref_by_doi,
        search_crossref_by_text,
        search_scopus_by_title,
        search_scholar_by_title,
        search_scholar_by_ref_text,
        search_s2_by_title,
        search_openalex_by_title,
        check_url_availability
    )
except ModuleNotFoundError as e:
    # 這裡就是您原本缺少 except 的地方
    st.error(f"❌ 程式啟動失敗：找不到必要的模組檔案。請檢查 'modules' 資料夾是否已完整上傳。\n錯誤訊息: {e}")
    st.stop()

# ==============================================================================
# 3. Streamlit 頁面設定與主程式
# ==============================================================================
st.set_page_config(
    page_title="Citation Verification Report Tool",
    page_icon="📊",
    layout="wide"
)

st.markdown("""
<style>
    .main-header { font-size: 2.2rem; font-weight: bold; text-align: center; color: #4F46E5; margin-bottom: 5px; }
    .sub-header { text-align: center; color: #6B7280; margin-bottom: 2rem; }
    .status-badge { padding: 4px 10px; border-radius: 12px; font-size: 0.85em; font-weight: bold; }
    .ref-box { background-color: #F9FAFB; padding: 12px; border-radius: 8px; font-family: 'Courier New', monospace; font-size: 0.9em; border: 1px solid #E5E7EB; margin-top: 5px; }
    .report-card { background-color: #FFFFFF; padding: 20px; border-radius: 10px; border: 1px solid #E5E7EB; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
</style>
""", unsafe_allow_html=True)

# ========== Session State ==========
if "results" not in st.session_state:
    st.session_state.results = []

# ========== Core Utility Functions ==========
def format_name_field(data):
    if not data:
        return None
    try:
        if isinstance(data, str):
            if not (data.startswith('[') or data.startswith('{')):
                return data
            try:
                data = ast.literal_eval(data)
            except:
                return data
        names_list = []
        data_list = data if isinstance(data, list) else [data]
        for item in data_list:
            if isinstance(item, dict):
                parts = [item.get('family', ''), item.get('given', '')]
                names_list.append(", ".join([p for p in parts if p]))
            else:
                names_list.append(str(item))
        return "; ".join(names_list)
    except:
        return str(data)

def refine_parsed_data(parsed_item):
    item = parsed_item.copy()
    raw_text = item.get('text', '').strip()

    # 如果沒有 URL，嘗試從文字中抓取
    if not item.get('url'):
        url_match = re.search(r'(https?://[^\s]+)', raw_text)
        if url_match:
            item['url'] = url_match.group(1).strip(' .')

    for key in ['doi', 'url', 'title', 'date']:
        if item.get(key) and isinstance(item[key], str):
            item[key] = item[key].strip(' ,.;)]}>')

    title = item.get('title', '')

    # 修正 title 開頭為 "&" 或 "and" 的情況
    if title and (title.startswith('&') or title.lower().startswith('and ')):
        fix_match = re.search(
            r'^&(?:amp;)?\s*[^0-9]+?\(?\d{4}\)?[\.\s]+(.*)',
            title
        )
        if fix_match:
            cleaned_title = fix_match.group(1).strip()
            if len(cleaned_title) > 5:
                title = cleaned_title
                item['title'] = title

    if title:
        title = re.sub(r'^\s*\d{4}[\.\s]+', '', title)
        title = re.sub(r'(?i)\.?\s*arXiv.*$', '', title)
        title = re.sub(r'(?i)\.?\s*Available.*$', '', title)
        item['title'] = title

    # 如果 title 太短或遺失，嘗試使用其他欄位 fallback
    if not title or len(title) < 5:
        abbr_match = re.search(
            r'^([A-Z0-9\-\.\s]{2,12}:\s*.+?)(?=\s*[,\[]|\s*Available|\s*\(|\bhttps?://|\.|$)',
            raw_text
        )
        if abbr_match:
            item['title'] = abbr_match.group(1).strip()
        else:
            for backup_key in ['publisher', 'container-title', 'journal']:
                val = item.get(backup_key)
                if val and len(str(val)) > 15:
                    item['title'] = str(val).strip()
                    break

        if (not item.get('title') or item['title'] == 'N/A') and item.get('date'):
            year_str = str(item['date'])[0:4]
            if year_str.isdigit():
                fallback_match = re.search(rf'{year_str}\W+\s*(.+)', raw_text)
                if fallback_match:
                    candidate = fallback_match.group(1).strip()
                    candidate = re.sub(r'(?i)\.?\s*arXiv.*$', '', candidate)
                    candidate = re.sub(r'(?i)\.?\s*Available.*$', '', candidate)
                    if len(candidate) > 5:
                        item['title'] = candidate.strip(' .')

    url_val = item.get('url', '')
    if url_val:
        doi_match = re.search(
            r'(10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+)',
            url_val
        )
        if doi_match:
            item['doi'] = doi_match.group(1).strip('.')

    if item.get('authors'):
        item['authors'] = format_name_field(item['authors'])
    if item.get('editor'):
        item['editor'] = format_name_field(item['editor'])

    return item

def check_single_task(idx, raw_ref, local_df, target_col, scopus_key, serpapi_key):
    ref = refine_parsed_data(raw_ref)
    title, text = ref.get('title', ''), ref.get('text', '')
    search_query = title if (title and len(title) > 8) else text[:120]
    doi, parsed_url = ref.get('doi'), ref.get('url')
    first_author = (
        ref['authors'].split(';')[0].split(',')[0].strip()
        if ref.get('authors') else ""
    )

    res = {
        "id": idx,
        "title": title,
        "text": text,
        "parsed": ref,
        "sources": {},
        "found_at_step": None,
        "suggestion": None
    }

    # 0. Local Database Check
    if bool(re.search(r'[\u4e00-\u9fff]', search_query)) and local_df is not None and title:
        match_row, _ = search_local_database(
            local_df, target_col, title, threshold=0.85
        )
        if match_row is not None:
            res.update({
                "sources": {"Local DB": "Matched"},
                "found_at_step": "0. Local Database"
            })
            return res

    # 1. DOI Check
    if doi:
        _, url, _ = search_crossref_by_doi(
            doi, target_title=title if title else None
        )
        if url:
            res.update({
                "sources": {"Crossref": url},
                "found_at_step": "1. Crossref (DOI)"
            })
            return res

    # 1. Crossref Search
    url, _ = search_crossref_by_text(search_query, first_author)
    if url:
        res.update({
            "sources": {"Crossref": url},
            "found_at_step": "1. Crossref (Search)"
        })
        return res

    # 2. Scopus
    if scopus_key:
        url, _ = search_scopus_by_title(
            search_query, scopus_key, author=first_author
        )
        if url:
            res.update({
                "sources": {"Scopus": url},
                "found_at_step": "2. Scopus"
            })
            return res

    # 5. Google Scholar
    for api_func, step_name in [
        (lambda: search_scholar_by_title(
            search_query,
            serpapi_key,
            author=first_author,
            raw_text=raw_ref['text']
        ), "5. Google Scholar")
    ]:
        try:
            url, _ = api_func()
            if url:
                res.update({
                    "sources": {step_name.split(". ")[1]: url},
                    "found_at_step": step_name
                })
                return res
        except:
            pass

    # SerpAPI fallback suggestion
    if serpapi_key:
        url_r, _ = search_scholar_by_ref_text(
            text, serpapi_key, target_title=title
        )
        if url_r:
            res["suggestion"] = url_r

    # 6. Direct Link Check
    if parsed_url and parsed_url.startswith('http'):
        if check_url_availability(parsed_url):
            res.update({
                "sources": {"Direct Link": parsed_url},
                "found_at_step": "6. Website / Direct URL"
            })
        else:
            res.update({
                "sources": {"Direct Link (Dead)": parsed_url},
                "found_at_step": "6. Website (Link Failed)"
            })

    return res

# ========== Sidebar ==========
with st.sidebar:
    st.header("⚙️ System Settings")
    DEFAULT_CSV_PATH = "112ndltd.csv"
    local_df, target_col = None, None

    if os.path.exists(DEFAULT_CSV_PATH):
        local_df = load_csv_data(DEFAULT_CSV_PATH)
        if local_df is not None:
            st.success(f"✅ Local database loaded: {len(local_df)} records")
            target_col = (
                "論文名稱" if "論文名稱" in local_df.columns
                else local_df.columns[0]
            )

    scopus_key = get_scopus_key()
    serpapi_key = get_serpapi_key()
    st.divider()
    st.caption("API Status:")
    st.write(
        f"Scopus: {'✅' if scopus_key else '❌'} | "
        f"SerpAPI: {'✅' if serpapi_key else '❌'}"
    )

# ========== Main Page ==========
st.markdown(
    '<div class="main-header">📚 Automated Citation Verification Report</div>',
    unsafe_allow_html=True
)
st.markdown(
    '<div class="sub-header">'
    'Integrated academic databases with one-click verification and CSV export'
    '</div>',
    unsafe_allow_html=True
)

# Step 1: Input
st.markdown("### 📥 Step 1: Paste Reference List")
raw_input = st.text_area(
    "Paste your references here:",
    height=250,
    placeholder=(
        "Example:\n"
        "StyleTTS 2: Towards Human-Level Text-to-Speech...\n"
        "AIOS: LLM Agent Operating System..."
    )
)

# Step 2: Run
if st.button(
    "🚀 Run Automatic Verification & Generate Report",
    type="primary",
    use_container_width=True
):
    if not raw_input:
        st.warning("⚠️ Please paste references before running.")
    else:
        st.session_state.results = []
        with st.status(
            "🔍 Running verification process...",
            expanded=True
        ) as status:
            status.write("Parsing references...")
            _, struct_list = parse_references_with_anystyle(raw_input)

            if struct_list:
                status.write(
                    f"Querying academic databases "
                    f"({len(struct_list)} references)..."
                )
                progress_bar = st.progress(0)
                results_buffer = []

                with ThreadPoolExecutor(max_workers=20) as executor:
                    futures = {
                        executor.submit(
                            check_single_task,
                            i + 1,
                            r,
                            local_df,
                            target_col,
                            scopus_key,
                            serpapi_key
                        ): i
                        for i, r in enumerate(struct_list)
                    }
                    for i, future in enumerate(as_completed(futures)):
                        results_buffer.append(future.result())
                        progress_bar.progress(
                            (i + 1) / len(struct_list)
                        )

                st.session_state.results = sorted(
                    results_buffer,
                    key=lambda x: x['id']
                )
                status.update(
                    label="✅ Verification completed!",
                    state="complete",
                    expanded=False
                )
            else:
                st.error(
                    "❌ AnyStyle parsing failed. "
                    "Please check your input."
                )

# Step 3: Results & Download
if st.session_state.results:
    st.divider()
    st.markdown("### 📊 Step 2: Verification Results & Download")

    total_refs = len(st.session_state.results)
    verified_db = sum(
        1 for r in st.session_state.results
        if r.get('found_at_step') and "6." not in r.get('found_at_step')
    )
    failed_refs = total_refs - verified_db

    col1, col2, col3 = st.columns(3)
    col1.metric("Total References", total_refs)
    col2.metric("Verified via Databases", verified_db)
    col3.metric(
        "Require Manual Review",
        failed_refs,
        delta_color="inverse"
    )

    df_export = pd.DataFrame([{
        "ID": r['id'],
        "Status": (
            r['found_at_step']
            if r['found_at_step']
            else "Not Found"
        ),
        "Detected Title": r['title'],
        "Original Reference Text": r['text'],
        "Verified Source Link": (
            next(iter(r['sources'].values()), "N/A")
            if r['sources'] else "N/A"
        )
    } for r in st.session_state.results])

    csv_data = df_export.to_csv(
        index=False
    ).encode('utf-8-sig')

    st.download_button(
        label="📥 Download Full Verification Report (CSV)",
        data=csv_data,
        file_name=(
            f"Citation_Check_{time.strftime('%Y%m%d_%H%M')}.csv"
        ),
        mime="text/csv",
        use_container_width=True
    )

    # Step 4: Detailed List with Filters
    st.markdown("---")
    st.markdown("#### 🔍 Detailed Verification List")

    filter_option = st.radio(
        "Filter results:",
        [
            "Show All",
            "✅ Verified (Database)",
            "🌐 Valid Website Source",
            "⚠️ Website (Connection Failed)",
            "❌ Not Found"
        ],
        horizontal=True
    )

    filtered_results = []
    for r in st.session_state.results:
        raw_step = r.get('found_at_step')
        step = str(raw_step) if raw_step is not None else ""

        if filter_option == "Show All":
            filtered_results.append(r)
        elif (
            filter_option == "✅ Verified (Database)"
            and step and "6." not in step and "Failed" not in step
        ):
            filtered_results.append(r)
        elif (
            filter_option == "🌐 Valid Website Source"
            and "6." in step and "Failed" not in step
        ):
            filtered_results.append(r)
        elif (
            filter_option == "⚠️ Website (Connection Failed)"
            and "Failed" in step
        ):
            filtered_results.append(r)
        elif (
            filter_option == "❌ Not Found"
            and (not step or step == "")
        ):
            filtered_results.append(r)

    if not filtered_results:
        st.info(
            f"No items match the filter: "
            f"'{filter_option}'."
        )
    else:
        for item in filtered_results:
            raw_step = item.get('found_at_step')
            step = str(raw_step) if raw_step is not None else ""

            if not step:
                status_icon = "❌"
            elif "Failed" in step:
                status_icon = "⚠️"
            elif "6." in step:
                status_icon = "🌐"
            else:
                status_icon = "✅"

            with st.expander(
                f"{status_icon} ID {item['id']}: "
                f"{item['text'][:80]}..."
            ):
                st.markdown(
                    f"**Verification Result:** "
                    f"`{step if step else 'No Match Found'}`"
                )
                st.markdown("**Original Reference:**")
                st.markdown(
                    f"<div class='ref-box'>{item['text']}</div>",
                    unsafe_allow_html=True
                )

                if item.get('sources'):
                    st.markdown("**Source Links:**")
                    for src, link in item['sources'].items():
                        st.write(f"- {src}: {link}")

                if (
                    (not step or "Failed" in step)
                    and item.get("suggestion")
                ):
                    st.warning(
                        "💡 Suggested similar reference "
                        f"[Click to review manually]"
                        f"({item['suggestion']})"
                    )
else:
    st.info(
        "💡 No results yet. "
        "Paste references above and click the button to start."
    )