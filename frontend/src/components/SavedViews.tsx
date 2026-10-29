import { useEffect, useState } from 'react'
import { Bookmark, Pencil, Plus, Trash2, X } from 'lucide-react'
import type { QueryResponse, SavedViewResponse } from '../api-types'
import { api } from '../api'

type Edit = {kind:'create'} | {kind:'rename'|'delete';view:SavedViewResponse}

export function SavedViews({result,version,disabled,onApply}:{result?:QueryResponse|null;version:number;disabled:boolean;onApply:(id:string)=>Promise<void>}) {
  const [views,setViews]=useState<SavedViewResponse[]>([])
  const [edit,setEdit]=useState<Edit|null>(null)
  const [name,setName]=useState('')
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState('')
  async function reload(){setViews(await api<SavedViewResponse[]>('/saved-views'))}
  useEffect(()=>{let active=true;void api<SavedViewResponse[]>('/saved-views').then(items=>{if(active)setViews(items)}).catch(e=>{if(active)setError(e.message)});return()=>{active=false}},[])
  function open(value:Edit){setEdit(value);setName(value.kind==='create'?'':value.view.name);setError('')}
  async function submit(){
    if(!edit)return
    setBusy(true);setError('')
    try{
      if(edit.kind==='create')await api('/saved-views',{name,result_set_id:result?.result_set_id,expected_context_version:version})
      else if(edit.kind==='rename')await api(`/saved-views/${edit.view.id}/rename`,{name,expected_version:edit.view.version})
      else await api(`/saved-views/${edit.view.id}/delete`,{expected_version:edit.view.version})
      await reload();setEdit(null)
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  return <section className="saved-views"><div className="saved-views-heading"><span><Bookmark size={14}/>常用查询</span><button aria-label="保存当前查询" disabled={!result||disabled||busy} onClick={()=>open({kind:'create'})}><Plus size={13}/>保存当前查询</button></div>
    <div className="view-chips">{views.map(view=><div className="view-chip" key={view.id}><button aria-label={`应用常用查询 ${view.name}`} disabled={disabled||busy} onClick={()=>void onApply(view.id)}>{view.name}</button><button aria-label={`重命名常用查询 ${view.name}`} disabled={disabled||busy} onClick={()=>open({kind:'rename',view})}><Pencil size={11}/></button><button aria-label={`移除常用查询 ${view.name}`} disabled={disabled||busy} onClick={()=>open({kind:'delete',view})}><Trash2 size={11}/></button></div>)}{!views.length&&<span className="small muted">保存已应用的筛选和排序，下次登录仍可使用。</span>}</div>
    {error&&!edit&&<div className="error" role="alert">{error}</div>}
    {edit&&<div className="modal-backdrop"><section className="modal small-modal" role="dialog" aria-modal="true" aria-label="常用查询设置"><div className="section-head"><h2>{edit.kind==='create'?'保存当前查询':edit.kind==='rename'?'重命名常用查询':'移除常用查询'}</h2><button className="icon-button" aria-label="关闭常用查询设置" disabled={busy} onClick={()=>setEdit(null)}><X size={18}/></button></div>
      {edit.kind==='delete'?<p>移除“{edit.view.name}”？此操作只移除个人查询，不修改订单。</p>:<><label className="modal-field">查询名称<input aria-label="查询名称" value={name} maxLength={60} autoFocus onChange={e=>setName(e.target.value)}/></label><p>保存已应用的条件和排序；日期范围保持原值。应用时会重新查询最新数据。</p></>}
      {error&&<div className="error" role="alert">{error}</div>}<div className="modal-actions"><button className="button secondary" disabled={busy} onClick={()=>setEdit(null)}>取消</button><button className="button primary" disabled={busy||(edit.kind!=='delete'&&!name.trim())} onClick={()=>void submit()}>{edit.kind==='delete'?'确认移除':'保存查询'}</button></div></section></div>}
  </section>
}
