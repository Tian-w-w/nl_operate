import { useEffect, useState } from 'react'
import type { KnowledgeResponse, KnowledgeSource, StatisticResponse } from '../api-types'
import { api } from '../api'

function Source({source}:{source:KnowledgeSource}) {
  return <details className="knowledge-source"><summary>{source.title} · {source.section}{source.is_demo&&<span className="knowledge-demo">演示</span>}</summary><blockquote>{source.excerpt}</blockquote><small>资料版本 {source.version}</small></details>
}

export function AssistantEvidence({knowledge,statistic}:{knowledge?:KnowledgeResponse|null;statistic?:StatisticResponse|null}) {
  return <>
    {knowledge&&<div className="knowledge-evidence" aria-label="知识回答依据"><div className="evidence-label">{knowledge.mode==='no_evidence'?'知识库未找到依据':knowledge.mode==='retrieval_only'?'知识库检索原文':'检索增强回答 · 可展开来源'}</div>{knowledge.sources.map(source=><Source key={source.source_id} source={source}/>)}</div>}
    {statistic&&<section className="statistic-evidence" aria-label="城市去重统计"><div><strong>{statistic.distinct_count}</strong><span>个不同收货城市</span></div><p>{statistic.basis}</p><details><summary>查看城市及订单数量</summary><ul>{statistic.values.map(item=><li key={item.value}><span>{item.value}</span><strong>{item.order_count} 条</strong></li>)}</ul></details></section>}
  </>
}

export function KnowledgeLibrary() {
  const [sources,setSources]=useState<KnowledgeSource[]|null>(null)
  const [error,setError]=useState('')
  useEffect(()=>{let active=true;void api<KnowledgeSource[]>('/knowledge/sources').then(items=>{if(active)setSources(items)}).catch(e=>{if(active)setError((e as Error).message)});return()=>{active=false}},[])
  return <section className="knowledge-library" aria-label="业务知识库"><h2>业务知识库</h2><p>可询问发货时限、默认快递等规则。回答只依据当前身份可访问的资料，来源会随回答展示。</p><div className="knowledge-notice">内置资料为演示规则，不是公司正式规定。正式使用前，请替换项目 data/knowledge/ 中的文档及其清单配置。</div>{error&&<div className="error" role="alert">{error}</div>}{sources===null&&!error&&<p>正在读取资料…</p>}{sources?.length===0&&<p>当前身份没有可用的知识文档。</p>}{sources?.map(source=><Source key={source.source_id} source={source}/>)}</section>
}
