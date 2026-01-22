# app.py (All-in-One Version)

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
import requests
import urllib3
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

# 嘗試匯入 SerpAPI，如果沒安裝則提供假物件避免報錯
try:
    from serpapi import GoogleSearch
except ImportError:
    GoogleSearch = None

# ==============================================================================
# 0. Streamlit 頁面設定 (必須是第一個 Streamlit 指令)
# ==============================================================================
st.set_page_config(
    page_title="Citation Verification Tool",
    page_icon="📚",
    layout="wide"
)

# ==============================================================================
# 1. 環境設定與 AnyStyle 安裝邏輯
# ==============================================================================
def install_and_setup_anystyle():
    """
    自動檢測並修復 AnyStyle 執行環境 (Ruby Gem)
    """
    if "anystyle_setup_done" in st.session_state:
        return

    # print 用於後台 Log，不會影響 Streamlit 介面順序
    print("🔄 [System] 初始化 AnyStyle 環境...")
    
    # 步驟 A: 安裝 (僅使用 user install 以避開權限問題)
    if shutil.which("anystyle") is None:
        try:
            print("⚠️ 尚未偵測到指令，開始安裝...")
            subprocess.run(
                ["gem", "install", "--user-install", "anystyle-cli"], 
                check=True, 
                capture_output=True
            )
            print("✅ Gem 使用者安裝成功")
        except Exception as e:
            print(f"❌ 安裝失敗 (可能是網路或 Ruby 未安裝): {e}")

    # 步驟 B: 路徑修復 (將 Gem bin 加入 PATH)
    try:
        # 詢問 Ruby 使用者目錄在哪
        ruby_user_dir = subprocess.check_output(
            ["ruby", "-e", "puts Gem.user_dir"], 
            text=True
        ).strip()
        
        user_bin_path = os.path.join(ruby_user_dir, "bin")
        current_path = os.environ.get("PATH", "")
        
        if user_bin_path not in current_path:
            print(f"🔧 修復 PATH: {user_bin_path}")
            os.environ["PATH"] += os.pathsep + user_bin_path
            
    except Exception:
        pass
        
    st.session_state["anystyle_setup_done"] = True

# 執行環境檢查
install_and_setup_anystyle()

# ==============================================================================
# 2. 功能模組：Parser (整合自 parsers.py)
# ==============================================================================

def get_anystyle_path():
    # 優先使用系統找到的
    path = shutil.which("anystyle")
    if path: return path
    # 備用：推算使用者路徑
    try:
        user_dir = subprocess.check_output(["ruby", "-e", "puts Gem.user_dir"], text=True).strip()
        return os.path.join(user_dir, "bin", "anystyle")
    except:
        return "anystyle"

ANYSTYLE_CMD = get_anystyle_path()

def clean_title(text):
    if not text: return ""
    # 正規化 unicode (例如將 full-width 轉 half-width)
    text = unicodedata.normalize("NFKC", str(text))
    # 移除破折號等干擾字元
    dash_chars = ["-", "–", "—", "−", "‐"]
    for d in dash_chars:
        text = text.replace(d, "")
    # 只保留字母與數字，轉小寫
    cleaned = [ch.lower() for ch in text if unicodedata.category(ch)[0] in ("L", "N", "Z")]
    return re.sub(r"\s+", " ", "".join(cleaned)).strip()

def parse_references_with_anystyle(raw_text):
    if not raw_text or not raw_text.strip():
        return [], []

    lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
    structured_refs = []
    
    # 這裡假設 custom.mod 在同一目錄下，如果沒有就忽略
    use_custom_model = os.path.exists("custom.mod")

    # 建立 UI 進度條
    progress_bar = st.progress(0)
    
    for i, line in enumerate(lines):
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', line))
        
        cmd = [ANYSTYLE_CMD]
        if has_chinese and use_custom_model:
            cmd.extend(["-P", "custom.mod"])
        cmd.extend(["-f", "json", "parse"])
        
        try:
            # 使用 stdin 傳入資料 (比寫檔快且穩)
            process = subprocess.run(
                cmd,
                input=line,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True
            )
            
            output = process.stdout.strip()
            # 擷取 JSON
            if not output.startswith("["):
                 match = re.search(r"\[.*\]", output, re.DOTALL)
                 if match: output = match.group(0)
            
            data = ast.literal_eval(output) # 或 json.loads
            
            for item in data:
                # 簡易資料清洗
                for k, v in item.items():
                    if isinstance(v, list):
                        # author 欄位特殊處理
                        if k == 'author':
                            authors = []
                            for a in v:
                                if isinstance(a, dict):
                                    parts = [p for p in [a.get("given"), a.get("family")] if p]
                                    authors.append(" ".join(parts))
                                else:
                                    authors.append(str(a))
                            item["authors"] = ", ".join(authors)
                        else:
                            item[k] = "; ".join([str(x) for x in v])

                item["text"] = line
                # 確保有 title
                if "title" not in item: item["title"] = "N/A"
                
                structured_refs.append(item)
                
        except Exception as e:
            # 失敗時保留原始文字
            structured_refs.append({"text": line, "title": "Parse Error", "error": str(e)})

        progress_bar.progress((i + 1) / len(lines))

    return lines, structured_refs

# ==============================================================================
# 3. 功能模組：Local DB (整合自 local_db.py)
# ==============================================================================

def load_csv_data(uploaded_file):
    if uploaded_file is None:
        return None
    try:
        return pd.read_csv(uploaded_file)
    except UnicodeDecodeError:
        try:
            uploaded_file.seek(0)
            return pd.read_csv(uploaded_file, encoding='big5')
        except Exception as e:
            st.error(f"讀取 CSV 失敗: {e}")
            return None

def search_local_database(df, title_column, query_title, threshold=0.8):
    if df is None or not title_column or not query_title:
        return None, None

    clean_query = clean_title(query_title)
    best_score = 0
    best_match_row = None

    # 簡單遍歷搜尋
    for index, row in df.iterrows():
        db_title = str(row[title_column])
        clean_db_title = clean_title(db_title)
        
        # 快速過濾
        if clean_query in clean_db_title or clean_db_title in clean_query:
            score = 1.0
        else:
            score = SequenceMatcher(None, clean_query, clean_db_title).ratio()
        
        if score > best_score:
            best_score = score
            best_match_row = row

    if best_score >= threshold:
        return best_match_row, best_score
    return None, 0

# ==============================================================================
# 4. 功能模組：API Clients (整合自 api_clients.py)
# ==============================================================================

S2_API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_API_URL = "https://api.openalex.org/works"
MAX_RETRIES = 2
TIMEOUT = 10

def _read_key_file(filename):
    try:
        with open(filename, "r") as f: return f.read().strip()
    except: return None

def get_scopus_key():
    return st.secrets.get("scopus_api_key") or _read_key_file("scopus_key.txt")

def get_serpapi_key():
    return st.secrets.get("serpapi_key") or _read_key_file("serpapi_key.txt")

def _check_author_match(query_author, result_authors_list):
    """
    作者比對邏輯 (Zhang, X. vs L. Zhang)
    """
    if not query_author or len(query_author) < 2: return True 
    
    query_author = query_author.lower().strip()
    q_family = ""
    q_given_initial = ""

    if "," in query_author:
        parts = query_author.split(",")
        q_family = parts[0].strip()
        if len(parts) > 1 and parts[1].strip():
            q_given_initial = parts[1].strip()[0] 
    else:
        parts = query_author.split()
        q_family = parts[-1].strip()
        if len(parts) > 1:
            q_given_initial = parts[0].strip()[0]

    common_names = ['wang', 'chen', 'lee', 'li', 'zhang', 'liu', 'lin', 'yang', 'huang', 'wu', 'smith', 'jones']
    is_common_name = (q_family in common_names)

    for auth in result_authors_list:
        r_family = ""
        r_given_initial = ""
        r_full = ""

        if isinstance(auth, dict):
            r_family = str(auth.get('family') or auth.get('surname') or '').lower()
            given = str(auth.get('given') or auth.get('initials') or '').lower()
            if given: r_given_initial = given[0]
            r_full = f"{given} {r_family}".strip()
        else:
            r_full = str(auth).lower()
            if " " in r_full:
                r_family = r_full.split()[-1]
                r_given_initial = r_full.split()[0][0]
            else:
                r_family = r_full

        if q_family == r_family or q_family in r_full:
            if is_common_name and q_given_initial and r_given_initial:
                if q_given_initial != r_given_initial:
                    continue 
            return True
    return False

def _is_match(query, result):
    if not query or not result: return False
    c_q = clean_title(query)
    c_r = clean_title(result)
    
    def remove_noise(text):
        text = re.sub(r'\b(19|20)\d{2}\b', '', text)
        text = re.sub(r'\b(arxiv|biorxiv|available|online|access)\b', '', text, flags=re.IGNORECASE)
        return " ".join(text.split())

    c_q = remove_noise(c_q)
    c_r = remove_noise(c_r)

    if len(c_q) > len(c_r) * 1.5:
        if c_r in c_q: return True

    ratio = SequenceMatcher(None, c_q, c_r).ratio()
    if ratio >= 0.65: return True
    
    q_words = set(c_q.split())
    r_words = set(c_r.split())
    stop_words = {'a', 'an', 'the', 'of', 'in', 'for', 'with', 'on', 'at', 'by', 'and', 'from', 'to'}
    missing = [w for w in q_words if w not in stop_words and w not in r_words]
    
    if len(missing) <= 1 and len(q_words) >= 5: return True
    if len(missing) == 0 and len(c_q) > len(c_r) * 0.3: return True

    return False

def _call_external_api_with_retry(url, params, headers=None):
    if not headers: headers = {'User-Agent': 'ReferenceChecker/1.0'}
    for _ in range(MAX_RETRIES):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
            if response.status_code == 200: return response.json(), "OK"
            if response.status_code in [401, 403]: return None, f"Auth Error ({response.status_code})"
        except: pass
    return None, "Error"

# --- 各個 API 實作 ---

def search_crossref_by_doi(doi, target_title=None):
    if not doi: return None, None, "Empty DOI"
    clean_doi = doi.strip(' ,.;)]}>')
    url = f"https://api.crossref.org/works/{clean_doi}"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            item = response.json().get("message", {})
            titles = item.get("title", [])
            res_title = titles[0] if titles else ""
            if target_title and not _is_match(target_title, res_title):
                return None, None, f"DOI Title Mismatch"
            return res_title, item.get("URL") or f"https://doi.org/{clean_doi}", "OK"
        return None, None, f"HTTP {response.status_code}"
    except: return None, None, "Conn Error"

def search_crossref_by_text(title, author=None):
    if not title: return None, "Empty Title"
    params = {'query.bibliographic': title, 'rows': 2}
    if author: params['query.author'] = author
    data, status = _call_external_api_with_retry("https://api.crossref.org/works", params)
    
    if status == "OK" and data and data.get('message', {}).get('items'):
        for item in data['message']['items']:
            res_title = item.get('title', [''])[0]
            res_authors = item.get('author', [])
            if _is_match(title, res_title):
                if _check_author_match(author, res_authors):
                    return item.get('URL') or f"https://doi.org/{item.get('DOI')}", "OK"
                else: continue
        return None, "Match failed"
    return None, status

def search_scopus_by_title(title, api_key, author=None):
    if not api_key: return None, "No API Key"
    url = "https://api.elsevier.com/content/search/scopus"
    headers = {"Accept": "application/json", "X-ELS-APIKey": api_key}
    params = {"query": f'TITLE("{title}")', "count": 1}
    data, status = _call_external_api_with_retry(url, params, headers)
    if status == "OK" and data:
        entries = data.get('search-results', {}).get('entry', [])
        if not entries or 'error' in entries[0]: return None, "(No results)"
        match = entries[0]
        res_title = match.get('dc:title', '')
        res_creator = match.get('dc:creator', '')
        if _is_match(title, res_title):
            if _check_author_match(author, [res_creator]):
                return match.get('prism:url', 'https://www.scopus.com'), "OK"
            else: return None, "Author Mismatch"
        else: return None, "Title Mismatch"
    return None, "Error"

def search_scholar_by_title(title, api_key, author=None, raw_text=None):
    if not api_key or not GoogleSearch: return None, "No API Key or SerpLib"
    
    def _do_search(query_string, match_mode, required_author=None):
        try:
            params = {"engine": "google_scholar", "q": query_string, "api_key": api_key, "num": 10}
            results = GoogleSearch(params).get_dict()
            organic = results.get("organic_results", [])
            for res in organic:
                res_title = res.get("title", "")
                if _is_match(title, res_title):
                    if required_author:
                        pub_info = res.get("publication_info", {})
                        summary = pub_info.get("summary", "")
                        extracted = summary.split(" - ")[0] if " - " in summary else summary
                        if not _check_author_match(required_author, [a.strip() for a in extracted.split(",")]):
                            continue
                    found_link = res.get("link")
                    if found_link: return found_link, match_mode
                    else: return "Citation Record (No Direct Link)", match_mode + " [Citation Only]"
            return None, None
        except Exception as e: return None, f"Error: {e}"

    valid_author = None
    if author:
        cleaned = re.sub(r'(?i)[\(\[]?\bet\.?\s*al\.?[\)\]]?', '', author).strip().strip(' .,;()[]')
        if len(cleaned) > 1: valid_author = cleaned

    if valid_author:
        link, status = _do_search(f'{title} {valid_author}', "match (Title+Author)")
        if link: return link, status

    link, status = _do_search(title, "match (Title Only)", required_author=valid_author)
    if link: return link, status
    
    if raw_text and len(raw_text) > 10:
        link, status = _do_search(raw_text, "match (Raw Text Fallback)", required_author=valid_author)
        if link: return link, status

    return None, "No match found"

def search_scholar_by_ref_text(ref_text, api_key, target_title=None):
    if not api_key or not GoogleSearch: return None, "No API Key"
    params = {"engine": "google_scholar", "q": ref_text, "api_key": api_key, "num": 1}
    try:
        results = GoogleSearch(params).get_dict()
        organic = results.get("organic_results", [])
        if organic:
            res_title = organic[0].get("title", "")
            if target_title and not _is_match(target_title, res_title):
                return None, "Title mismatch"
            return organic[0].get("link"), "similar"
    except: pass
    return None, "No results"

def check_url_availability(url):
    if not url or not url.startswith("http"): return False
    
    # 過濾明顯非論文頁面的短網址 (例如純首頁)
    if url.count('/') < 3: return False
    
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    # 偽裝成一般瀏覽器的 User-Agent
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    try:
        # 1. 先嘗試 HEAD 請求 (較快)
        resp = requests.head(
            url, 
            headers=headers, 
            timeout=5, 
            allow_redirects=True, 
            verify=False
        )
        if 200 <= resp.status_code < 400:
            return True
            
        # 2. 如果 HEAD 失敗 (例如 403/404/405)，嘗試 GET 請求 (較慢但準確)
        # 很多學術網站不支援 HEAD
        resp = requests.get(
            url, 
            headers=headers, 
            timeout=8, # GET 比較慢，給多一點時間
            allow_redirects=True, 
            verify=False,
            stream=True # 只下載標頭和一點點內容，不用下載整頁
        )
        if 200 <= resp.status_code < 400:
            return True
            
    except Exception:
        pass
        
    return False

# ==============================================================================
# 5. 主程式核心邏輯 (check_single_task)
# ==============================================================================

def format_name_field(data):
    # 簡單格式化
    if not data: return None
    if isinstance(data, list): return "; ".join(map(str, data))
    return str(data)

def refine_parsed_data(parsed_item):
    item = parsed_item.copy()
    raw_text = item.get('text', '').strip()
    
    if not item.get('url'):
        url_match = re.search(r'(https?://[^\s]+)', raw_text)
        if url_match: item['url'] = url_match.group(1).strip(' .')

    for key in ['doi', 'url', 'title', 'date']:
        if item.get(key) and isinstance(item[key], str):
            item[key] = item[key].strip(' ,.;)]}>')

    title = item.get('title', '')
    if title:
        title = re.sub(r'^\s*\d{4}[\.\s]+', '', title)
        title = re.sub(r'(?i)\.?\s*arXiv.*$', '', title)
        item['title'] = title
        
    return item

def check_single_task(idx, raw_ref, local_df, target_col, scopus_key, serpapi_key):
    ref = refine_parsed_data(raw_ref)
    title, text = ref.get('title', ''), ref.get('text', '')
    search_query = title if (title and len(title) > 8) else text[:120]
    doi, parsed_url = ref.get('doi'), ref.get('url')
    
    # 提取第一作者
    authors_str = ref.get('authors', '')
    first_author = authors_str.split(';')[0].split(',')[0].strip() if authors_str else ""

    res = {
        "id": idx,
        "title": title,
        "text": text,
        "parsed": ref,
        "sources": {},
        "found_at_step": None,
        "suggestion": None
    }

    # 0. Local Database
    if bool(re.search(r'[\u4e00-\u9fff]', search_query)) and local_df is not None and title:
        match_row, _ = search_local_database(local_df, target_col, title, threshold=0.85)
        if match_row is not None:
            res.update({"sources": {"Local DB": "Matched"}, "found_at_step": "0. Local Database"})
            return res

    # 1. DOI / Crossref
    if doi:
        _, url, _ = search_crossref_by_doi(doi, target_title=title if title else None)
        if url:
            res.update({"sources": {"Crossref": url}, "found_at_step": "1. Crossref (DOI)"})
            return res

    url, _ = search_crossref_by_text(search_query, first_author)
    if url:
        res.update({"sources": {"Crossref": url}, "found_at_step": "1. Crossref (Search)"})
        return res

    # 2. Scopus
    if scopus_key:
        url, _ = search_scopus_by_title(search_query, scopus_key, author=first_author)
        if url:
            res.update({"sources": {"Scopus": url}, "found_at_step": "2. Scopus"})
            return res

    # 5. Google Scholar
    if serpapi_key:
        url, step_name = search_scholar_by_title(search_query, serpapi_key, author=first_author, raw_text=text)
        if url:
            res.update({"sources": {"Scholar": url}, "found_at_step": f"5. Google Scholar ({step_name})"})
            return res

        # Fallback suggestion
        url_r, _ = search_scholar_by_ref_text(text, serpapi_key, target_title=title)
        if url_r: res["suggestion"] = url_r

    # 6. Direct Link Check
    if parsed_url and parsed_url.startswith('http'):
        if check_url_availability(parsed_url):
            res.update({"sources": {"Direct Link": parsed_url}, "found_at_step": "6. Website"})
        else:
            res.update({"sources": {"Direct Link (Dead)": parsed_url}, "found_at_step": "6. Website (Failed)"})

    return res

# ==============================================================================
# 6. UI 主程式
# ==============================================================================

# Sidebar
with st.sidebar:
    st.header("⚙️ System Settings")
    
    # 環境診斷
    if shutil.which("anystyle"):
        st.success("✅ AnyStyle Ready")
    else:
        st.error("❌ AnyStyle Missing")
        st.info("請確認 packages.txt 已包含 ruby-full 並重啟 APP")

    DEFAULT_CSV_PATH = "112ndltd.csv"
    local_df, target_col = None, None
    if os.path.exists(DEFAULT_CSV_PATH):
        local_df = load_csv_data(DEFAULT_CSV_PATH)
        if local_df is not None:
            st.success(f"Local DB: {len(local_df)} records")
            target_col = "論文名稱" if "論文名稱" in local_df.columns else local_df.columns[0]

    scopus_key = get_scopus_key()
    serpapi_key = get_serpapi_key()
    st.write(f"Scopus: {'✅' if scopus_key else '❌'} | SerpAPI: {'✅' if serpapi_key else '❌'}")

# Main
st.markdown('<h1 style="text-align:center; color:#4F46E5;">📚 自動化文獻驗證系統</h1>', unsafe_allow_html=True)
st.markdown('<p style="text-align:center;">整合 AnyStyle 解析與多重資料庫 (Crossref, Scopus, Google Scholar) 驗證</p>', unsafe_allow_html=True)

raw_input = st.text_area("請貼上參考文獻列表 (Paste References):", height=200, placeholder="例如: StyleTTS 2: Towards Human-Level Text-to-Speech...")

if st.button("🚀 開始驗證", type="primary", use_container_width=True):
    if not raw_input:
        st.warning("⚠️ 請輸入內容")
    else:
        st.session_state.results = []
        with st.status("🔍 執行中...", expanded=True) as status:
            status.write("正在解析格式 (AnyStyle)...")
            _, struct_list = parse_references_with_anystyle(raw_input)
            
            if struct_list:
                status.write(f"解析成功 ({len(struct_list)} 筆)，開始查詢資料庫...")
                progress_bar = st.progress(0)
                results_buffer = []

                with ThreadPoolExecutor(max_workers=5) as executor:
                    futures = {
                        executor.submit(
                            check_single_task, i+1, r, local_df, target_col, scopus_key, serpapi_key
                        ): i for i, r in enumerate(struct_list)
                    }
                    for i, future in enumerate(as_completed(futures)):
                        results_buffer.append(future.result())
                        progress_bar.progress((i + 1) / len(struct_list))

                st.session_state.results = sorted(results_buffer, key=lambda x: x['id'])
                status.update(label="✅ 驗證完成！", state="complete", expanded=False)
            else:
                status.update(label="❌ 解析失敗", state="error")
                st.error("AnyStyle 解析失敗，請檢查輸入格式或系統環境。")

# 結果顯示
if "results" in st.session_state and st.session_state.results:
    st.divider()
    
    total = len(st.session_state.results)
    verified = sum(1 for r in st.session_state.results if r.get('found_at_step') and "6." not in r.get('found_at_step'))
    
    c1, c2, c3 = st.columns(3)
    c1.metric("總筆數", total)
    c2.metric("資料庫驗證成功", verified)
    c3.metric("需人工確認", total - verified)

# 下載 CSV
    df_export = pd.DataFrame([{
        "ID": r['id'],
        "Status": r['found_at_step'] or "Not Found",
        "Title": r['title'],
        "Source": next(iter(r['sources'].values()), "N/A") if r['sources'] else "N/A",
        "Original": r['text']
    } for r in st.session_state.results])
    
    st.download_button("📥 下載報告 (CSV)", df_export.to_csv(index=False).encode('utf-8-sig'), "report.csv", "text/csv")

    # 詳細列表
st.markdown("### 📝 詳細結果")
    for item in st.session_state.results:
        step = item.get('found_at_step', '')
        # 如果是 Parse Error，顯示紅色警示
        icon = "❌" if "Parse Error" in item['title'] or (step and "Failed" in step) or not step else "✅"
        
        with st.expander(f"{icon} [{item['id']}] {item['title']}"):
            # 1. 如果有錯誤訊息，優先顯示 (這是除錯的關鍵！)
            if item.get('error'):
                st.error(f"🔧 系統錯誤訊息: {item['error']}")
                st.info("💡 提示: 如果是 'No such file'，請確認 packages.txt 是否存在並已 Reboot App。")

            st.write(f"**狀態**: {step or '未找到'}")
            st.write(f"**原始文字**: {item['text']}")
            
            if item.get('sources'): 
                st.write(f"**連結**: {item['sources']}")
                
            if item.get('suggestion'): 
                st.warning(f"💡 建議參考: {item['suggestion']}")