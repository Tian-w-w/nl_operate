"""Tenant-scoped lexical retrieval and grounded answer validation; no model calls here."""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

from .config import ROOT
from .service import BusinessError

KNOWLEDGE_ROOT = ROOT / 'data/knowledge'
NO_EVIDENCE = '当前知识库没有足够依据回答这个问题，请补充已确认的业务规则或联系人工确认。'


def tokens(text):
    text = text.lower().replace('收获', '收货')
    for phrase in ['请问', '告诉我', '是什么', '什么', '一下', '我们', '你们', '订单', '公司']:
        text = text.replace(phrase, '')
    terms = re.findall(r'[a-z0-9]+', text)
    for part in re.findall(r'[\u4e00-\u9fff]+', text):
        terms.extend(part[i:i+2] for i in range(len(part)-1))
    for group in [('发货', '出库', '寄出', '发运'), ('快递', '承运', '配送'), ('时限', '多久', '时间', '时效')]:
        if any(word in text for word in group):
            terms.extend(group)
    return Counter(terms)


def chunks(tenant, root=None):
    root = Path(root or KNOWLEDGE_ROOT).resolve()
    manifest = root / 'manifest.json'
    if not manifest.exists():
        return []
    try:
        documents = json.loads(manifest.read_text(encoding='utf-8'))['documents']
        if not isinstance(documents, list) or len(documents) > 64:
            raise ValueError('Invalid knowledge manifest')
        result, seen = [], set()
        for doc in documents:
            if tenant not in doc['tenants']:
                continue
            ident = doc['id']
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}', ident) or ident in seen:
                raise ValueError('Invalid/duplicate knowledge ID')
            seen.add(ident)
            path = (root / doc['file']).resolve()
            if not path.is_relative_to(root) or path.suffix != '.md' or path.stat().st_size > 100_000:
                raise ValueError('Invalid knowledge path/size')
            content = path.read_text(encoding='utf-8')
            version = hashlib.sha256(content.encode()).hexdigest()[:12]
            for section in re.split(r'(?m)^## ', content)[1:]:
                title, _, body = section.partition('\n')
                for index, offset in enumerate(range(0, len(body.strip()), 700)):
                    excerpt = body.strip()[offset:offset+700]
                    result.append({'source_id': f'{ident}:{len(result)}:{index}', 'title': doc['title'],
                                   'section': title.strip(), 'version': version, 'is_demo': bool(doc.get('is_demo', False)), 'excerpt': excerpt})
                    if len(result) > 256:
                        raise ValueError('Knowledge chunk limit exceeded')
        return result
    except (ValueError, KeyError, TypeError, OSError):
        raise BusinessError('KNOWLEDGE_UNAVAILABLE', '知识库配置或文档无效，请检查知识库文件。', 503) from None


def retrieve(question, tenant, root=None):
    """Small BM25 index built from approved Markdown; Chinese bigrams need no tokenizer dependency."""
    entries = chunks(tenant, root)
    query = tokens(question)
    if not entries or not query:
        return []
    counts = [tokens(item['title']+' '+item['section']+' '+item['excerpt']) for item in entries]
    lengths = [sum(item.values()) for item in counts]
    average = sum(lengths)/len(lengths) or 1
    frequencies = Counter(term for item in counts for term in item if term in query)
    scores = []
    for entry, count, length in zip(entries, counts, lengths):
        score = 0
        for term in query:
            if term not in count:
                continue
            frequency = frequencies[term]
            idf = math.log(1+(len(entries)-frequency+.5)/(frequency+.5))
            score += idf*count[term]*2.2/(count[term]+1.2*(.25+.75*length/average))
        if score > .2:
            scores.append((score, entry))
    return [item for _, item in sorted(scores, key=lambda pair: (-pair[0], pair[1]['source_id']))[:3]]


def grounded_answer(references, answer=None, citation_ids=None, generated=False):
    if not references:
        return {'answer': NO_EVIDENCE, 'grounded': False, 'mode': 'no_evidence', 'sources': []}
    if generated:
        by_id = {item['source_id']: item for item in references}
        if not answer or not citation_ids or any(ident not in by_id for ident in citation_ids):
            raise BusinessError('KNOWLEDGE_CITATION_INVALID', '模型回答没有引用本次检索到的有效来源，已停止。', 422)
        used = [by_id[ident] for ident in dict.fromkeys(citation_ids)]
    else:
        used = references
        answer = '\n\n'.join(f"{item['section']}：{item['excerpt']}" for item in used)
    if any(item['is_demo'] for item in used):
        answer = '以下依据演示知识库，不是公司正式规定。\n'+answer
    return {'answer': answer, 'grounded': True, 'mode': 'retrieval_augmented_generation' if generated else 'retrieval_only', 'sources': used}
