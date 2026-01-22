# modules/parsers.py
import re
import unicodedata
import subprocess
import json
import streamlit as st
import tempfile
import os
import shutil

# ==============================================================================
# 輔助函式：自動尋找 AnyStyle 執行檔的絕對路徑
# ==============================================================================
def get_anystyle_path():
    # 1. 如果系統 PATH 裡直接找得到，就回傳指令名稱
    if shutil.which("anystyle"):
        return "anystyle"
    
    # 2. 如果找不到，嘗試詢問 Gem 的 bin 目錄在哪裡
    gem_path = shutil.which("gem")
    if gem_path:
        try:
            # 執行 `gem environment bin` 取得安裝路徑
            gem_bin_dir = subprocess.check_output(
                [gem_path, "environment", "bin"], 
                text=True
            ).strip()
            
            # 組合出絕對路徑
            candidate = os.path.join(gem_bin_dir, "anystyle")
            
            # 確認該檔案真的存在
            if os.path.exists(candidate):
                return candidate
                
            # 有些系統會加 .bat 或 .cmd (Windows)，雖然雲端是 Linux 但保留彈性
            if os.path.exists(candidate + ".bat"):
                return candidate + ".bat"
                
        except Exception as e:
            print(f"嘗試尋找 Gem路徑失敗: {e}")
    
    # 3. 如果還是找不到，嘗試常見的 Linux 使用者路徑 (Streamlit Cloud 常見位置)
    home = os.path.expanduser("~")
    common_paths = [
        os.path.join(home, ".local/share/gem/ruby/3.0.0/bin/anystyle"), # 版本可能不同
        os.path.join(home, ".gem/ruby/3.0.0/bin/anystyle"),
        "/usr/local/bin/anystyle",
        "/usr/bin/anystyle"
    ]
    
    for path in common_paths:
        # 使用 glob 模糊搜尋版本號可能比較好，但這裡先試固定路徑
        if os.path.exists(path):
            return path
            
    # 真的找不到，回傳預設值讓它報錯，但至少我們盡力了
    return "anystyle"

# 預先取得路徑 (模組載入時執行一次即可)
ANYSTYLE_CMD = get_anystyle_path()

# ==============================================================================
# AnyStyle 解析主程式
# ==============================================================================

def parse_references_with_anystyle(raw_text_for_anystyle):
    """
    將文獻列表拆分處理，支援自動路徑偵測。
    """
    if not raw_text_for_anystyle or not raw_text_for_anystyle.strip():
        return [], []

    # 顯示目前使用的指令路徑 (除錯用，成功後可註解)
    # st.write(f"🔧 Debug: 使用的 AnyStyle路徑: `{ANYSTYLE_CMD}`")

    lines = [line.strip() for line in raw_text_for_anystyle.split('\n') if line.strip()]
    
    structured_refs = []
    raw_texts = []

    progress_bar = st.progress(0)
    total_lines = len(lines)

    for i, line in enumerate(lines):
        # 語言判定
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', line))

        # 建立暫存檔
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                delete=False,
                encoding="utf-8"
            ) as tmp:
                tmp.write(line)
                tmp_path = tmp.name
        except Exception as e:
            st.error(f"❌ 無法建立暫存檔：{e}")
            continue

        # 組合指令
        # 使用我們找到的絕對路徑 ANYSTYLE_CMD
        command = [ANYSTYLE_CMD]

        if has_chinese:
            # 確保 custom.mod 存在，否則不加參數以免報錯
            if os.path.exists("custom.mod"):
                command.extend(["-P", "custom.mod"])
        
        command.extend(["-f", "json", "parse", tmp_path])

        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True
            )

            stdout = process.stdout.strip()

            # 擷取 JSON
            if not stdout.startswith("["):
                match = re.search(r"\[.*\]", stdout, re.DOTALL)
                if match:
                    stdout = match.group(0)

            line_data = json.loads(stdout)

            for item in line_data:
                cleaned_item = {}
                for key, value in item.items():
                    if isinstance(value, list):
                        if key == "author":
                            authors = []
                            for a in value:
                                if isinstance(a, dict):
                                    parts = [p for p in [a.get("given"), a.get("family")] if p]
                                    authors.append(" ".join(parts))
                                else:
                                    authors.append(str(a))
                            cleaned_item["authors"] = ", ".join(authors)
                        else:
                            cleaned_item[key] = " ".join(map(str, value))
                    else:
                        cleaned_item[key] = value

                if "text" not in cleaned_item:
                    cleaned_item["text"] = line

                structured_refs.append(cleaned_item)
                raw_texts.append(cleaned_item["text"])

        except Exception as e:
            st.error(f"解析第 {i+1} 行時發生錯誤：{e}")
            # 如果還是找不到檔案，提供詳細建議
            if isinstance(e, FileNotFoundError):
                st.warning(
                    f"💡 診斷資訊：\n"
                    f"1. 系統嘗試執行的指令是: `{ANYSTYLE_CMD}`\n"
                    f"2. 請確認 packages.txt 是否包含 `ruby-full`\n"
                    f"3. 請嘗試重啟 App (Reboot)"
                )
                
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        
        progress_bar.progress((i + 1) / total_lines)

    return raw_texts, structured_refs


# ==============================================================================
# 標題清洗函式 (保持原樣)
# ==============================================================================

def clean_title(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    dash_chars = ["-", "–", "—", "−", "‐", "-"]
    for d in dash_chars:
        text = text.replace(d, "")
    cleaned = [
        ch.lower()
        for ch in text
        if unicodedata.category(ch)[0] in ("L", "N", "Z")
    ]
    return re.sub(r"\s+", " ", "".join(cleaned)).strip()

def clean_title_for_remedial(text):
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    dash_chars = ["-", "–", "—", "−", "‐", "-"]
    for d in dash_chars:
        text = text.replace(d, "")
    text = re.sub(r"\b\d+\b", "", text)
    cleaned = [
        ch.lower()
        for ch in text
        if unicodedata.category(ch)[0] in ("L", "N", "Z")
    ]
    return re.sub(r"\s+", " ", "".join(cleaned)).strip()