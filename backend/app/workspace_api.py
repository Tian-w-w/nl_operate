from fastapi import APIRouter, Depends
from sqlalchemy import select

from .models import SavedView
from .schemas import (AnalyticsRequest, AnalyticsResponse, AuditQuery, AuditResponse, OrderDetail, QueryResponse,
                      SavedViewCreate, SavedViewRename, SavedViewResponse, SavedViewVersion, ViewApply)
from .service import BusinessError
from .workspace import analytics, apply_view, create_view, full_detail, own_view, query_audit, rename_view, view_response


def workspace_router(transaction,authenticated,settings):
    router = APIRouter(prefix='/api')

    @router.get('/orders/{order_id}/full',response_model=OrderDetail)
    def detail(order_id:int,session=Depends(authenticated,scope='function'),db=Depends(transaction,scope='function')):
        return full_detail(db,session,order_id,settings)

    @router.post('/analytics/orders',response_model=AnalyticsResponse)
    def insight(body:AnalyticsRequest,session=Depends(authenticated,scope='function'),db=Depends(transaction,scope='function')):
        return analytics(db,session,body,settings)

    @router.post('/audit/query',response_model=AuditResponse)
    def audit(body:AuditQuery,session=Depends(authenticated,scope='function'),db=Depends(transaction,scope='function')):
        return query_audit(db,session,body)

    @router.get('/saved-views',response_model=list[SavedViewResponse])
    def views(session=Depends(authenticated,scope='function'),db=Depends(transaction,scope='function')):
        rows = db.scalars(select(SavedView).where(SavedView.tenant_id == session.tenant_id,SavedView.actor_id == session.actor_id)
                          .order_by(SavedView.updated_at.desc(),SavedView.id.asc()))
        return [view_response(item) for item in rows]

    @router.post('/saved-views',response_model=SavedViewResponse)
    def save(body:SavedViewCreate,session=Depends(authenticated,scope='function'),db=Depends(transaction,scope='function')):
        return create_view(db,session,body)

    @router.post('/saved-views/{view_id}/apply',response_model=QueryResponse,response_model_exclude_unset=True)
    def apply(view_id:str,body:ViewApply,session=Depends(authenticated,scope='function'),db=Depends(transaction,scope='function')):
        return apply_view(db,session,view_id,body.expected_context_version,settings)

    @router.post('/saved-views/{view_id}/rename',response_model=SavedViewResponse)
    def rename(view_id:str,body:SavedViewRename,session=Depends(authenticated,scope='function'),db=Depends(transaction,scope='function')):
        return rename_view(db,session,view_id,body)

    @router.post('/saved-views/{view_id}/delete')
    def delete(view_id:str,body:SavedViewVersion,session=Depends(authenticated,scope='function'),db=Depends(transaction,scope='function')):
        item = own_view(db,session,view_id)
        if item.version != body.expected_version:
            raise BusinessError('VIEW_VERSION_CONFLICT','常用查询已变化，请刷新列表')
        db.delete(item)
        return {'deleted':True,'id':view_id}

    return router
