# modules/api_clients.py
import streamlit as st
import requests
import time
from difflib import SequenceMatcher
from serpapi import GoogleSearch
import urllib3
import re

# 導入標題清洗函式
from .parsers import clean_title

# --- 全域 API 設定 ---
S2_API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_API_URL = "https://api.openalex.org/works"

MAX_RETRIES = 2
TIMEOUT = 10

# ========== API Key 管理 ==========
def get_scopus_key():
    return st.secrets.get("scopus_api_key") or _read_key_file("scopus_key.txt")

def get_serpapi_key():
    return st.secrets.get("serpapi_key") or _read_key_file("serpapi_key.txt")

def _read_key_file(filename):
    try:
        with open(filename, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

# ========== [核心] 1. 作者比對邏輯 (新增) ==========
# 修改 modules/api_clients.py 中的 _check_author_match

def _check_author_match(query_author, result_authors_list):
    """
    嚴格比對函式：專門解決 Zhang, X. 與 L. Zhang 被誤判為同一人的問題。
    邏輯：
    1. 拆解輸入作者的 姓 (Family) 與 名 (Given)。
    2. 針對常見姓氏 (Zhang, Li 等)，強制檢查名字首字母是否一致。
    3. 如果首字母不同 (X vs L)，直接視為不同人。
    """
    if not query_author or len(query_author) < 2:
        return True 
    
    query_author = query_author.lower().strip()
    
    # --- 步驟 1: 解析您的輸入 (例如: "Zhang, X.") ---
    # 預設變數
    q_family = ""
    q_given_initial = ""

    if "," in query_author:
        # 格式: "Zhang, X." -> 逗號前是姓，逗號後是名
        parts = query_author.split(",")
        q_family = parts[0].strip()
        # 取名字的第一個字母作為 Initial (例如 "X." -> "x")
        if len(parts) > 1 and parts[1].strip():
            q_given_initial = parts[1].strip()[0] 
    else:
        # 格式: "X. Zhang" -> 最後一個字是姓
        parts = query_author.split()
        q_family = parts[-1].strip()
        if len(parts) > 1:
            q_given_initial = parts[0].strip()[0]

    # 定義必須嚴格檢查的大姓
    common_names = ['wang', 'chen', 'lee', 'li', 'zhang', 'liu', 'lin', 'yang', 'huang', 'wu', 'smith', 'jones']
    
    # 判斷是否為大姓 (如果是，我們就一定要對首字母)
    is_common_name = (q_family in common_names)

    # --- 步驟 2: 檢查系統抓到的作者列表 (例如: ["L. Zhang", "L. Xu", ...]) ---
    for auth in result_authors_list:
        r_family = ""
        r_given_initial = ""
        r_full = ""

        # 解析 API 回傳的作者格式
        if isinstance(auth, dict):
            r_family = str(auth.get('family') or auth.get('surname') or '').lower()
            given = str(auth.get('given') or auth.get('initials') or '').lower()
            if given: r_given_initial = given[0]
            r_full = f"{given} {r_family}".strip()
        else:
            r_full = str(auth).lower()
            # 簡單拆解字串 (假設 "L. Zhang")
            if " " in r_full:
                r_family = r_full.split()[-1] # 抓 "zhang"
                r_given_initial = r_full.split()[0][0] # 抓 "l"
            else:
                r_family = r_full

        # --- 步驟 3: 關鍵比對 (Zhang vs Zhang) ---
        
        # 先比對姓氏
        if q_family == r_family or q_family in r_full:
            
            # 如果是大姓 (Zhang)，且雙方都有名字首字母 -> 進行嚴格檢查！
            if is_common_name and q_given_initial and r_given_initial:
                
                # 這裡就是抓出 "X" != "L" 的關鍵！
                if q_given_initial != r_given_initial:
                    # 雖然姓對了，但名字首字母不對，視為「撞名」，跳過這個作者
                    continue 
            
            # 如果通過了上面的檢查 (或者是冷門姓氏不用查那麼細)，才算找到
            return True
            
    # 找遍了列表裡的所有人，沒有一個符合「姓氏對 + 首字母也對」的
    return False

# ========== [核心] 2. 標題比對邏輯 (包含您之前的寬鬆優化) ==========
# 在 modules/api_clients.py 中找到 _is_match 函式並修改

def _is_match(query, result):
    if not query or not result: return False
    c_q = clean_title(query)
    c_r = clean_title(result)
    if not c_q or not c_r: return False
        
    # --- 新增：強效去噪 ---
    # 移除常見的非標題字眼，避免它們導致比對失敗
    def remove_noise(text):
        # 移除 4位數年份 (如 2023, 2024)
        text = re.sub(r'\b(19|20)\d{2}\b', '', text)
        # 移除 arXiv, bioRxiv, Available, Online 等字眼
        text = re.sub(r'\b(arxiv|biorxiv|available|online|access)\b', '', text, flags=re.IGNORECASE)
        # 移除多餘空白
        return " ".join(text.split())

    c_q = remove_noise(c_q)
    c_r = remove_noise(c_r)
    # ---------------------

    # 1. 針對 Query 是長段落... (維持原樣)
    if len(c_q) > len(c_r) * 1.5:
        if c_r in c_q: return True

    # 2. 相似度比對 (維持原樣)
    ratio = SequenceMatcher(None, c_q, c_r).ratio()
    if ratio >= 0.65: return True  # 建議稍微調降到 0.8 以容忍少許差異
    
    # 3. 關鍵字比對
    q_words = set(c_q.split())
    r_words = set(c_r.split())
    stop_words = {'a', 'an', 'the', 'of', 'in', 'for', 'with', 'on', 'at', 'by', 'and', 'from', 'to'} # 增加一些介係詞
    
    # ... (中間省略) ...

    # 反向檢查 (Query 的重要單字都在 Result 裡)
    missing_important_in_result = [w for w in q_words if w not in stop_words and w not in r_words]
    
    # --- 新增：容錯機制 ---
    # 如果只差 1 個字，且那個字很短或是數字，我們就當作它是雜訊，予以通過
    if len(missing_important_in_result) <= 1:
        # 如果 Query 很長，容許 1 個字的誤差是合理的
        if len(q_words) >= 5: 
            return True
    # ---------------------

    if len(missing_important_in_result) == 0:
        if len(c_q) > len(c_r) * 0.3:
            return True

    return False

# --- API 呼叫輔助 ---
def _call_external_api_with_retry(url: str, params: dict, headers=None):
    if not headers: headers = {'User-Agent': 'ReferenceChecker/1.0'}
    for _ in range(MAX_RETRIES):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
            if response.status_code == 200: return response.json(), "OK"
            if response.status_code in [401, 403]: return None, f"Auth Error ({response.status_code})"
        except: pass
    return None, "Error"

# ========== 1. Crossref (含作者比對) ==========

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
                return None, None, f"DOI Title Mismatch: {res_title[:40]}..."
                
            return res_title, item.get("URL") or f"https://doi.org/{clean_doi}", "OK"
        return None, None, f"HTTP {response.status_code}"
    except: return None, None, "Conn Error"

def search_crossref_by_text(title, author=None):
    if not title: return None, "Empty Title"
    params = {'query.bibliographic': title, 'rows': 2} # 抓前2筆增加機會
    if author:
        params['query.author'] = author # Crossref 支援直接搜作者
        
    data, status = _call_external_api_with_retry("https://api.crossref.org/works", params)
    
    if status == "OK" and data and data.get('message', {}).get('items'):
        for item in data['message']['items']:
            res_title = item.get('title', [''])[0]
            res_authors = item.get('author', []) # 取得作者列表
            
            # 雙重檢查：標題要對 + 作者要對
            if _is_match(title, res_title):
                if _check_author_match(author, res_authors):
                    return item.get('URL') or f"https://doi.org/{item.get('DOI')}", "OK"
                else:
                    # 如果標題對但作者不對，繼續找下一筆 (可能剛好是同名文章)
                    continue 
                    
        return None, "Match failed (Title or Author mismatch)"
    return None, status

# ========== 2. Scopus (新增作者比對) ==========

def search_scopus_by_title(title, api_key, author=None):
    """
    注意：app.py 呼叫此函式時，建議更新傳入 author 參數
    """
    if not api_key: return None, "No API Key"
    url = "https://api.elsevier.com/content/search/scopus"
    headers = {"Accept": "application/json", "X-ELS-APIKey": api_key}
    params = {"query": f'TITLE("{title}")', "count": 1}
    
    data, status = _call_external_api_with_retry(url, params, headers)
    
    if status == "OK" and data:
        entries = data.get('search-results', {}).get('entry', [])
        if not entries or 'error' in entries[0]:
            return None, "(No results found)"
        
        match = entries[0]
        res_title = match.get('dc:title', '')
        
        # Scopus 的作者通常在 'dc:creator' (第一作者) 或需要另外解析
        # Search API 的簡單回應通常只給 'dc:creator'
        res_creator = match.get('dc:creator', '')
        
        if _is_match(title, res_title):
            if _check_author_match(author, [res_creator]):
                return match.get('prism:url', 'https://www.scopus.com'), "OK"
            else:
                return None, f"Author Mismatch (Found: {res_creator})"
        else:
            return None, f"Title Mismatch: {res_title[:30]}..."
            
    return None, "Error"

# ========== 3. Google Scholar (無作者欄位，維持原樣) ==========


def search_scholar_by_title(title, api_key, author=None, raw_text=None):
    """
    階層式搜尋策略 (適應混合格式) - 修正版：
    1. 增加搜尋筆數至 10 筆，避免漏抓。
    2. 在標題補救與全文補救階段，強制檢查作者，避免抓錯人。
    """
    if not api_key: return None, "No API Key"
    
    # 內部搜尋小工具 (已升級支援作者驗證)
    def _do_search(query_string, match_mode, required_author=None):
        try:
            params = {"engine": "google_scholar", "q": query_string, "api_key": api_key, "num": 10}
            results = GoogleSearch(params).get_dict()
            organic = results.get("organic_results", [])
            
            for res in organic:
                res_title = res.get("title", "")
                
                if _is_match(title, res_title):
                    # 1. 作者驗證 (維持剛剛給您的修正)
                    if required_author:
                        pub_info = res.get("publication_info", {})
                        summary = pub_info.get("summary", "")
                        extracted_authors_str = summary.split(" - ")[0] if " - " in summary else summary
                        res_authors_list = [a.strip() for a in extracted_authors_str.split(",")]
                        
                        if not _check_author_match(required_author, res_authors_list):
                            continue

                    # 2. [關鍵修正] 處理沒有連結的情況
                    # 如果有 link 就回傳 link，如果沒有 (例如純引用)，就回傳 SerpAPI 的 result_id 或標記
                    found_link = res.get("link")
                    if found_link:
                        return found_link, match_mode
                    else:
                        # 雖然沒有連結，但我們確實找到了！回傳一個標記字串
                        return "Citation Record (No Direct Link)", match_mode + " [Citation Only]"
            
            return None, None
        except Exception as e:
            return None, f"Error: {e}"

    # ==========================================
    # 步驟 0: 智慧清洗作者
    # ==========================================
    valid_search_author = None
    if author:
        # 1. 先把 (et al), [et al], et al. 全部拿掉
        cleaned = re.sub(r'(?i)[\(\[]?\bet\.?\s*al\.?[\)\]]?', '', author).strip()
        # 2. 清理乾淨後，把頭尾多餘的標點符號修剪掉
        cleaned = cleaned.strip(' .,;()[]')
        if len(cleaned) > 1:
            valid_search_author = cleaned

    # ==========================================
    # 步驟 1: 標題 + 作者 (最準確，不需額外驗證)
    # ==========================================
    # 這裡搜尋時已經把作者加進去了，所以 Google 回傳的通常是對的，這裡維持原樣
    if valid_search_author:
        link, status = _do_search(f'{title} {valid_search_author}', "match (Title+Author)")
        if link: return link, status

    # ==========================================
    # 步驟 2: 純標題 (寬鬆補救) -> [修正] 加入作者驗證
    # ==========================================
    # 搜尋時只用標題，但篩選結果時「必須」檢查作者 (如果有提供作者的話)
    link, status = _do_search(title, "match (Title Only)", required_author=valid_search_author)
    if link: return link, status

    # ==========================================
    # 步驟 3: 原始全文 (終極保底) -> [修正] 加入作者驗證
    # ==========================================
    if raw_text and len(raw_text) > 10:
        link, status = _do_search(raw_text, "match (Raw Text Fallback)", required_author=valid_search_author)
        if link: return link, status

    return None, "No match found after 3 attempts"

def search_scholar_by_ref_text(ref_text, api_key, target_title=None):
    if not api_key: return None, "No API Key"
    params = {"engine": "google_scholar", "q": ref_text, "api_key": api_key, "num": 1}
    try:
        results = GoogleSearch(params).get_dict()
        organic = results.get("organic_results", [])
        if organic:
            res_title = organic[0].get("title", "")
            if target_title and not _is_match(target_title, res_title):
                return None, "Title mismatch in fallback"
            return organic[0].get("link"), "similar"
    except: pass
    return None, "No results"

# ========== 4. Semantic Scholar & OpenAlex (含作者比對) ==========

def search_s2_by_title(title, author=None):
    # 增加請求 'authors' 欄位
    params = {'query': title, 'limit': 1, 'fields': 'title,url,authors'}
    data, status = _call_external_api_with_retry(S2_API_URL, params)
    if status == "OK" and data.get('data'):
        match = data['data'][0]
        res_title = match.get('title')
        res_url = match.get('url')
        res_authors = match.get('authors', []) # S2 回傳 [{'authorId':..., 'name': '...'}]

        if _is_match(title, res_title):
            if _check_author_match(author, res_authors):
                return res_url, "OK"
            return None, "Author mismatch"
            
        return None, "Match failed"
    return None, status

def search_openalex_by_title(title, author=None):
    params = {'search': title, 'per_page': 1}
    data, status = _call_external_api_with_retry(OPENALEX_API_URL, params)
    
    if status == "OK" and data.get('results'):
        match = data['results'][0]
        res_title = match.get('title')
        # OpenAlex 作者結構: 'authorships': [{'author': {'display_name': '...'}}]
        res_authors = []
        for authorship in match.get('authorships', []):
            if 'author' in authorship:
                res_authors.append(authorship['author'].get('display_name', ''))

        if _is_match(title, res_title):
            if _check_author_match(author, res_authors):
                url = match.get('doi') or match.get('id')
                if url: return url, "OK"
                return None, "No Link"
            return None, "Author mismatch"
            
        return None, "Title mismatch"
            
    return None, status if status != "OK" else "No results found"

def check_url_availability(url):
    # 這裡加入您提過的：過濾純首頁 (例如 https://www.sans.org)
    if not url or not url.startswith("http"): return False
    
    # 簡單過濾：如果路徑只有 domain，極大機率是首頁而非論文頁
    # 邏輯：計算 '/' 的數量。https://abc.com 只有 2 個 '/'。https://abc.com/paper 有 3 個。
    if url.count('/') < 3: 
        return False
        
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        resp = requests.head(url, timeout=5, allow_redirects=True, verify=False)
        return 200 <= resp.status_code < 400

    except: return False