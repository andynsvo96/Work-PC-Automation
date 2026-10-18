document.addEventListener('crm-copyright-designs-request', event => {
  if (!/\/(?:order\/\d{7}(?:[/?#]|$)|app(?:\/)?#\/order\/\d{7}(?:[/?#]|$))/.test(location.href)) return;
  let result;
  try { result = { scan: readCopyrightDesignIds() }; }
  catch (error) { result = { error: error.message }; }
  document.dispatchEvent(new CustomEvent('crm-copyright-designs-response', {
    detail: JSON.stringify({ id: event.detail, ...result })
  }));
});
