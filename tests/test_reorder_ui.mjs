import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../ui_panel.html', import.meta.url), 'utf8');
const functions = ['syncCrmProcessingCustomInputControls', 'queueCrmReorderStock'].map(name => {
  let start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0);
  if (source.slice(start - 6, start) === 'async ') start -= 6;
  return source.slice(start, source.indexOf('\n}', start) + 2);
}).join('\n');

function dashboard(type='orders', result={success:true,message:'Queued'}) {
  const elements = {};
  for (const id of ['crmProcessingCustomInputType','crmProcessingCustomLinkFields','crmProcessingCustomOrderFields','crmProcessingCustomLinkInput','crmProcessingCustomOrderIdsInput','crmReorderStockBtn','crmProcessingCustomInputError','crmProcessingCustomInputDialog']) elements[id]={style:{},value:'',textContent:'',close(){this.closed=true}};
  elements.crmProcessingCustomInputType.value=type;
  elements.crmProcessingCustomOrderIdsInput.value='5000001\nhttps://crm.example/order/5000002';
  const calls=[];
  const context=vm.createContext({document:{getElementById:id=>elements[id]},withButtonBusy:(_button,_label,fn)=>fn(),api:async(...args)=>{calls.push(args);return result},setStatus(){},loadQueue(){}});
  vm.runInContext(functions,context);
  return {elements,calls,context};
}

test('Reorder button belongs to the custom dialog and appears only for order inputs',()=>{
  const markup=source.match(/<dialog id='crmProcessingCustomInputDialog'[\s\S]*?<\/dialog>/)[0];
  assert.match(markup,/id='crmReorderStockBtn'[^>]*margin-right:auto[^>]*>Reorder Stock<\/button>/);
  const d=dashboard('link');d.context.syncCrmProcessingCustomInputControls();
  assert.equal(d.elements.crmReorderStockBtn.style.display,'none');
  d.elements.crmProcessingCustomInputType.value='orders';d.context.syncCrmProcessingCustomInputControls();
  assert.equal(d.elements.crmReorderStockBtn.style.display,'');
});

test('button queues the pasted snapshot as JSON without invoking other processing tools',async()=>{
  const d=dashboard();await d.context.queueCrmReorderStock();
  assert.equal(d.calls.length,1);
  const [url,opts]=d.calls[0];assert.equal(url,'/crm/reorder');assert.equal(opts.headers['Content-Type'],'application/json');
  assert.deepEqual(JSON.parse(opts.body),{processing_filter:'custom',custom_input_type:'orders',custom_order_ids:d.elements.crmProcessingCustomOrderIdsInput.value});
  assert.equal(d.elements.crmProcessingCustomInputDialog.closed,true);
});

test('list-link input cannot queue and a queue error keeps the input available',async()=>{
  const link=dashboard('link');await link.context.queueCrmReorderStock();assert.equal(link.calls.length,0);
  const d=dashboard('orders',{success:false,message:'Review input'});await d.context.queueCrmReorderStock();
  assert.equal(d.elements.crmProcessingCustomInputError.textContent,'Review input');assert.equal(d.elements.crmProcessingCustomInputDialog.closed,undefined);
});
