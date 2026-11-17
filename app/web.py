from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app import store

templates=Jinja2Templates(directory=str(ROOT/'app/templates'))

def render(request:Request,key):
    from app.main import DB_PATH
    run_id=request.query_params.get('run_id')
    if run_id:
        state=store.get(DB_PATH,run_id)
    else:
        state=store.save(DB_PATH,evaluate({'dataset':request.query_params.get('dataset','dirty')}))
    context={'meta':META,'key':key,'state':state,'view':state['views'][key]}
    if key=='refund' and 'point_ledger' in state:
        from app.modules.refund.late import closing_balances, period_boundary, PERIOD_MODELS
        official=store.balances(DB_PATH,state['manifest']['run_id']) or closing_balances(state)
        context['late']={
            'official':[{'member':m,'balance':b} for m,b in sorted(official.items())],
            'closing':[{'member':m,'balance':b} for m,b in sorted(closing_balances(state).items())],
            'posted':store.posted_refunds(DB_PATH,state['manifest']['run_id']),
            'boundary':period_boundary(state).isoformat(),
            'models':PERIOD_MODELS,
        }
    return templates.TemplateResponse(request=request,name=f'{key}.html',context=context)
