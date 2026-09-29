// The isolated content script cannot access Angular's page-world scopes.
document.addEventListener('crm-emb-methods-request', event => {
  let result;
  try { result = { designs: readEmbDesignMethods() }; }
  catch (error) { result = { error: error.message }; }
  document.dispatchEvent(new CustomEvent('crm-emb-methods-response', {
    detail: JSON.stringify({ id: event.detail, ...result })
  }));
});
