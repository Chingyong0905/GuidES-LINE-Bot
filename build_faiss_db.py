import os
import re
from typing import Dict, List, Optional

from langchain_community.document_loaders import TextLoader, PyPDFLoader, Docx2txtLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings


UPLOAD_DIR = "uploaded_docs"

# 你目前支援的四種標籤（依你說的）
VALID_TAGS = {
    "department_announcement",
    "faculty_lab",
    "scholarship",
    "course_requirement",
}

# 輸出：四個獨立資料庫資料夾（你也可以改成你想要的命名）
OUT_DIR_BY_TAG = {
    "department_announcement": "faiss_db_department_announcement",
    "faculty_lab": "faiss_db_faculty_lab",
    "scholarship": "faiss_db_scholarship",
    "course_requirement": "faiss_db_course_requirement",
}


TAG_PATTERN = re.compile(r"^\s*類型\s*[:：]\s*([A-Za-z0-9_]+)\s*$")


def parse_tag_from_text_first_line(text: str) -> Optional[str]:
    """
    從文字內容的「第一個非空行」解析 類型：xxx
    """
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = TAG_PATTERN.match(line)
        if not m:
            return None
        tag = m.group(1).strip()
        return tag if tag in VALID_TAGS else None
    return None


def parse_tag_from_file(path: str, ext: str) -> Optional[str]:
    """
    優先從檔案本身解析第一行標籤（特別是 txt 最可靠）。
    如果不是 txt，則回傳 None，後續改從 loader 內容第一行嘗試解析。
    """
    if ext == ".txt":
        try:
            with open(path, "r", encoding="utf-8") as f:
                # 讀「第一個非空行」
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    m = TAG_PATTERN.match(line)
                    if not m:
                        return None
                    tag = m.group(1).strip()
                    return tag if tag in VALID_TAGS else None
        except Exception:
            return None
    return None


def load_documents_grouped_by_tag(upload_dir: str) -> Dict[str, List]:
    """
    讀取 uploaded_docs 內的檔案，依標籤分組成 {tag: [Document, ...]}
    會把 tag 寫入每個 Document.metadata，便於日後 debug / 追蹤。
    """
    grouped: Dict[str, List] = {t: [] for t in VALID_TAGS}

    for fn in os.listdir(upload_dir):
        path = os.path.join(upload_dir, fn)
        if not os.path.isfile(path):
            continue

        ext = os.path.splitext(fn)[1].lower()

        # 選 loader
        try:
            if ext == ".txt":
                loader = TextLoader(path, encoding="utf-8")
            elif ext == ".pdf":
                loader = PyPDFLoader(path)
            elif ext == ".docx":
                loader = Docx2txtLoader(path)
            else:
                continue
        except Exception as e:
            print(f"Skip {fn} (loader init failed): {e}")
            continue

        # 先嘗試直接從檔案第一行拿 tag（txt 最穩）
        tag = parse_tag_from_file(path, ext)

        try:
            docs = loader.load()
        except Exception as e:
            print(f"Skip {fn} (load failed): {e}")
            continue

        # 若前面拿不到 tag，改從載入後內容的第一個非空行解析
        if tag is None and docs:
            tag = parse_tag_from_text_first_line(docs[0].page_content)

        if tag is None:
            print(f"Skip {fn} (no valid tag in first line). Expected one of: {sorted(VALID_TAGS)}")
            continue

        # 寫 metadata 並加入分組
        for d in docs:
            d.metadata = d.metadata or {}
            d.metadata["tag"] = tag
            d.metadata["source_file"] = fn
            d.metadata["source_path"] = path

        grouped[tag].extend(docs)
        print(f"Loaded: {fn}  -> tag={tag}")

    return grouped


def build_faiss_for_tag(tag: str, docs: List, emb, chunk_size: int = 900, chunk_overlap: int = 150):
    """
    單一 tag 建庫並輸出
    """
    if not docs:
        print(f"[{tag}] No documents. Skip building FAISS.")
        return

    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    split_docs = splitter.split_documents(docs)
    print(f"[{tag}] docs={len(docs)}, chunks={len(split_docs)}")

    out_dir = OUT_DIR_BY_TAG[tag]
    vs = FAISS.from_documents(split_docs, emb)
    vs.save_local(out_dir)
    print(f"[{tag}] OK: saved to {out_dir}/")


def main():
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    grouped = load_documents_grouped_by_tag(UPLOAD_DIR)

    # 至少要有一組有資料
    total = sum(len(v) for v in grouped.values())
    if total == 0:
        raise RuntimeError(
            f"No valid tagged documents found in {UPLOAD_DIR}. "
            f"Ensure first non-empty line is like '類型：department_announcement'."
        )

    emb = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

    for tag in sorted(VALID_TAGS):
        build_faiss_for_tag(tag, grouped[tag], emb, chunk_size=900, chunk_overlap=150)


if __name__ == "__main__":
    main()
