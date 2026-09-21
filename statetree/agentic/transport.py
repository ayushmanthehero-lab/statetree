"""Real loopback llama.cpp transport, with formatted-prompt token preflight."""
import json
import os
from urllib import request, error
from urllib.parse import urlsplit
from statetree.project import validate_model
from .store import encode


class ContextTooLarge(ValueError):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('Local model redirects are not allowed')


class LocalTransport:
    def __init__(self,config):
        self.config=validate_model(config)
        p=urlsplit(config['url']); self.base=f'{p.scheme}://{p.netloc}'
        self.opener=request.build_opener(request.ProxyHandler({}),NoRedirect())
        self.timeout=min(1800,max(5,float(os.environ.get('STATETREE_LOCAL_TIMEOUT','300'))))
        self.last_preflight=None
        self.before_generation=None

    def post(self,path,data):
        headers={'Content-Type':'application/json'}
        key=os.environ.get('STATETREE_LOCAL_API_KEY','')
        if key: headers['Authorization']='Bearer '+key
        req=request.Request(self.base+path,encode(data).encode(),headers=headers,method='POST')
        try:
            with self.opener.open(req,timeout=self.timeout) as response:
                raw=response.read(4*1024*1024+1)
        except error.HTTPError as exc:
            details=exc.read(1200).decode('utf-8','replace')
            raise RuntimeError(f'Local model HTTP {exc.code}: {details}') from exc
        if len(raw)>4*1024*1024: raise ValueError('Local model response exceeds 4 MiB')
        return json.loads(raw)

    def count(self,messages,tools):
        data={'messages':messages,'tools':tools,'tool_choice':'auto','parallel_tool_calls':False,
              'add_generation_prompt':True,'chat_template_kwargs':{'enable_thinking':False}}
        formatted=self.post('/apply-template',data)
        if type(formatted) is not dict or type(formatted.get('prompt')) is not str:
            raise ValueError('llama.cpp /apply-template returned no prompt')
        result=self.post('/tokenize',{'content':formatted['prompt'],'add_special':False,'parse_special':True})
        if type(result) is not dict or type(result.get('tokens')) is not list:
            raise ValueError('llama.cpp /tokenize returned no token list')
        self.last_preflight={'input_tokens':len(result['tokens']),'method':'llama.cpp formatted-prompt tokenizer'}
        return len(result['tokens'])

    def complete(self,messages,tools):
        count=self.count(messages,tools)
        # Reserve output and framing margin; never truncate the original task silently.
        if count+self.config['max_tokens']+96>self.config['context_window_limit']:
            raise ContextTooLarge('Objective, saved note and tools exceed the model context. Reduce the task or increase -ContextSize; no generation was sent.')
        if self.before_generation is not None: self.before_generation()
        return self.post('/v1/chat/completions',{
            'model':self.config['model_id'],'messages':messages,'tools':tools,'tool_choice':'auto',
            'parallel_tool_calls':False,'max_tokens':self.config['max_tokens'],'temperature':0.2,
            'stream':False,'cache_prompt':True,'chat_template_kwargs':{'enable_thinking':False}})
