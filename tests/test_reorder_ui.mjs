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

function reportDashboard(task) {
  const names = ['openQueueTaskReport', 'reorderConfirmationLinksHtml', 'esc', 'crmOrderUrl', 'crmOrderLinkHtml', 'crmOrderTextHtml', 'renderQueue', 'sortedQueueTasks', 'queueStatusLabel', 'queueTaskProgressLabel'];
  const reportFunctions = names.map(name => {
    const start = source.indexOf(`function ${name}(`);
    return source.slice(start, source.indexOf('\n}', start) + 2);
  }).join('\n');
  const elements = Object.fromEntries(['queueTaskReportDialog', 'queueTaskReportDialogTitle', 'queueTaskReportDialogSummary', 'queueTaskReportDialogBody'].map(id => [id, {dataset:{},innerHTML:'',textContent:'',showModal(){this.open=true},close(){this.open=false}}]));
  elements.queueList = {innerHTML:''};
  const context = vm.createContext({ URL, document: {getElementById:id=>elements[id]}, queuePayload: {tasks:[task]}, queueVisible:true,queueSortOrder:'latest',queueExpandedTaskId:null,applyPlatformCapabilities(){},queueTimeText(){return ''} });
  vm.runInContext(reportFunctions, context);
  return {elements,context};
}

test('View orders includes successful, failed, and waiting orders with confirmed purchase links',()=>{
  const d = reportDashboard({ id:'batch',label:'Reorder Stock — 3 orders',task_type:'crm.reorder_batch',status:'failed',result_context:{report:{message:'Reorder: 1 succeeded; 1 failed; 1 waiting.',order_details:[
    {order_id:'5000001',success:true,status:'Success',message:'Purchased',purchases:[{vendor:'S&S Activewear',reference:'98765432',url:'https://www.ssactivewear.com/checkout/orderconfirmation?orderguid=fixture'}]},
    {order_id:'5000002',success:false,status:'Failed',message:'CRM note did not persist',purchases:[{vendor:'SanMar',reference:'FIXTURE',url:'https://www.sanmar.com/checkout/orderConfirmation/fixture'}]},
    {order_id:'5000003',success:null,status:'Waiting',message:'Waiting to reorder.'},
  ]}}});
  d.context.openQueueTaskReport('batch');
  const html=d.elements.queueTaskReportDialogBody.innerHTML;
  for(const id of ['5000001','5000002','5000003']) assert.ok(html.includes(`/order/${id}`));
  assert.ok(html.includes('Success')); assert.ok(html.includes('CRM note did not persist')); assert.ok(html.includes('Waiting'));
  assert.ok(html.includes('href="https://www.ssactivewear.com/checkout/orderconfirmation?orderguid=fixture"'));
  assert.ok(html.includes('href="https://www.sanmar.com/checkout/orderConfirmation/fixture"'));
  assert.ok(html.includes('S&amp;S Activewear confirmation 98765432'));
  assert.ok(html.includes('rel="noopener noreferrer"'));
  assert.equal(d.elements.queueTaskReportDialog.open,true);
  assert.ok(d.elements.queueTaskReportDialogTitle.innerHTML.includes('Orders'));
});

test('an open order report updates as the queue receives completed results',()=>{
  const task={id:'batch',label:'Reorder Stock',task_type:'crm.reorder_batch',status:'running',result_context:{report:{order_details:[{order_id:'5000001',success:null,status:'Running',message:'Reordering stock.'}]}}};
  const d=reportDashboard(task);
  d.context.openQueueTaskReport('batch');
  assert.ok(d.elements.queueTaskReportDialogBody.innerHTML.includes('Running'));
  d.context.renderQueue({tasks:[{...task,status:'completed',result_context:{report:{message:'Reorder: 1 succeeded; 0 failed.',order_details:[{order_id:'5000001',success:true,status:'Success',message:'Stock reordered.'}]}}}]});
  assert.ok(d.elements.queueTaskReportDialogBody.innerHTML.includes('Success'));
  assert.ok(d.elements.queueTaskReportDialogSummary.textContent.includes('1 succeeded'));
  assert.equal(d.elements.queueTaskReportDialog.open,true);
  d.context.renderQueue({tasks:[]});
  assert.equal(d.elements.queueTaskReportDialog.open,false);
});

test('confirmation URLs are escaped and restricted to HTTPS supplier sites',()=>{
  const {context}=reportDashboard({});
  for(const url of ['javascript:alert(1)','https://www.ssactivewear.com.evil.test/','https://user@www.ssactivewear.com/','http://www.sanmar.com/','https://example.test/']) {
    assert.equal(context.reorderConfirmationLinksHtml({purchases:[{vendor:'S&S',url}]}),'');
  }
  const html=context.reorderConfirmationLinksHtml({purchases:[{vendor:'<script>',reference:'<img>',url:'https://www.sanmar.com/checkout/orderConfirmation?a=1&b=2'}]});
  assert.ok(html.includes('&amp;b=2')); assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(!html.includes('<script>')); assert.ok(!html.includes('<img>'));
});
