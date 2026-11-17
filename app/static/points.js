const sheet={busy:false};const output=document.querySelector('#result');document.querySelector('#calculate').addEventListener('click',async()=>{if(sheet.busy)return;sheet.busy=true;output.textContent='计算中…';try{const dataset=document.querySelector('#dataset').value;const response=await fetch(dataset===document.body.dataset.source?'/api/runs/'+document.body.dataset.runId+'/rerun':'/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset})});const data=await response.json();if(!response.ok)throw Error(data.detail);const url=new URL(location.href);url.searchParams.set('dataset',dataset);url.searchParams.set('run_id',data.manifest.run_id);location.href=url;}catch(e){output.textContent=e.message;}finally{sheet.busy=false;}});document.querySelector('#member-filter').addEventListener('input',e=>document.querySelectorAll('.primary-panel tbody tr').forEach(row=>row.hidden=!row.textContent.includes(e.target.value)));document.querySelectorAll('td[tabindex]').forEach(cell=>cell.addEventListener('focus',()=>document.querySelector('#selection').textContent=cell.textContent));document.querySelector('#dataset').value=new URL(location.href).searchParams.get('dataset')||document.querySelector('#dataset').value;
const filePicker=document.querySelector('#csv-file');
if(filePicker){
 const editor=document.querySelector('#csv-text');
 filePicker.addEventListener('change',async()=>{const file=filePicker.files[0];if(!file)return;if(file.size>2000000){editor.value='';alert('文件超过2MB，请缩小后导入');return;}const bytes=await file.arrayBuffer();try{editor.value=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch(e){editor.value=new TextDecoder('gb18030',{fatal:true}).decode(bytes);}});
 document.querySelector('#import-csv').addEventListener('click',async event=>{event.target.disabled=true;try{const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv_text:editor.value})});const data=await response.json();if(!response.ok)throw Error(data.detail);location.href=location.pathname+'?run_id='+data.manifest.run_id;}catch(e){editor.setCustomValidity(e.message);editor.reportValidity();}finally{event.target.disabled=false;}});
 editor.addEventListener('input',()=>editor.setCustomValidity(''));
}
const latePanel=document.querySelector('.late-panel');
if(latePanel){
 const runId=latePanel.dataset.run;const form=document.querySelector('#late-form');const result=document.querySelector('#late-result');
 const readForm=()=>Object.fromEntries(new FormData(form).entries());
 const cell=(value)=>String(value??'').replace(/[<>&]/g,ch=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[ch]));
 const renderRow=row=>`<tr><td>${cell(row.refund_id||row.id)}</td><td>${cell(row.ref)}</td><td>${cell(row.model_label)}</td><td>${cell(row.delta)}</td><td>${cell(row.expired_refund)}</td><td>${cell(row.balance??'')}</td><td>${cell(row.status||'预览')}</td></tr>`;
 const showError=message=>{result.textContent=message;};
 document.querySelector('#late-preview').addEventListener('click',async()=>{
  result.textContent='核对中…';
  try{const response=await fetch(`/api/runs/${runId}/late-refunds/preview`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refunds:[readForm()]})});const data=await response.json();if(!response.ok)throw Error(data.detail);const row=data.rows[0];document.querySelector('#late-rows').innerHTML=renderRow({...row,status:'预览'+(data.reconcile.passed?'':' · 对账未通过')});result.textContent=data.reconcile.passed?'对账通过，可入账':'对账未通过，禁止入账';}catch(e){showError(e.message);}
 });
 form.addEventListener('submit',async event=>{
  event.preventDefault();result.textContent='入账中…';
  try{const response=await fetch(`/api/runs/${runId}/late-refunds`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refund:readForm()})});const data=await response.json();if(!response.ok)throw Error(data.detail);const balance=(data.official_balances.find(x=>x.member===data.member)||{}).balance;document.querySelector('#late-rows').insertAdjacentHTML('afterbegin',renderRow({...data,balance}));result.textContent=`${data.replayed?'已存在，未重复记账':'已入账'}：本笔差额 ${data.delta}，过期损失 ${data.expired_refund}，当前余额 ${balance}`;if(!data.replayed){window.setTimeout(()=>location.reload(),650);}}catch(e){showError(e.message);}
 });
}
