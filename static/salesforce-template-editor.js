/* Local template editor. Raw HTML is kept until the user edits the visual body. */
(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  const editable = ['name', 'subject', 'body_html'];
  const sameDraft = (left, right) => left && right && editable.every((key) => left[key] === right[key]);

  function allowedUrl(value, image = false) {
    const url = String(value || '').trim();
    if (/^\[[A-Z][A-Z0-9_-]*\]$/.test(url)) return true;
    if (!image && url.startsWith('#')) return true;
    try {
      const parsed = new URL(url);
      if (!image) return ['https:', 'http:', 'mailto:', 'tel:'].includes(parsed.protocol);
      const host = parsed.hostname.toLowerCase().replace(/\.$/, '');
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password ||
          parsed.origin === window.location.origin || !host.includes('.') ||
          host.startsWith('[') || /(?:^|\.)(?:localhost|local|internal|lan|home)$/.test(host)) return false;
      const ip = host.split('.').map(Number);
      if (ip.length === 4 && ip.every((part) => Number.isInteger(part) && part >= 0 && part <= 255)) {
        if ([0, 10, 127].includes(ip[0]) || (ip[0] === 169 && ip[1] === 254) ||
            (ip[0] === 172 && ip[1] >= 16 && ip[1] <= 31) ||
            (ip[0] === 192 && ip[1] === 168) ||
            (ip[0] === 100 && ip[1] >= 64 && ip[1] <= 127)) return false;
      }
      return true;
    } catch (_) { return false; }
  }

  function previewDocument(html, frameVersion = null) {
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    // Filtering affects the preview only; merely viewing a template never
    // rewrites its stored source. The frame also blocks scripts and navigation.
    parsed.querySelectorAll('script,iframe,frame,frameset,object,embed,applet,base,meta,link,form,input,button,textarea,select,svg,math').forEach((node) => node.remove());
    for (const node of parsed.querySelectorAll('*')) {
      for (const attribute of Array.from(node.attributes)) {
        if (/^on/i.test(attribute.name) || ['srcdoc', 'autofocus', 'contenteditable', 'formaction'].includes(attribute.name.toLowerCase())) {
          node.removeAttribute(attribute.name);
        }
      }
      if (node.hasAttribute('href') && !allowedUrl(node.getAttribute('href'))) node.removeAttribute('href');
      if (node.hasAttribute('src') && !allowedUrl(node.getAttribute('src'), true)) node.removeAttribute('src');
    }
    const policy = parsed.createElement('meta');
    policy.httpEquiv = 'Content-Security-Policy';
    const imageOrigins = Array.from(new Set(Array.from(parsed.querySelectorAll('img[src]'))
      .map((node) => node.getAttribute('src')).filter((url) => allowedUrl(url, true) && /^https:/i.test(url))
      .map((url) => new URL(url).origin)));
    policy.content = "default-src 'none'; script-src 'none'; connect-src 'none'; img-src " +
      (imageOrigins.join(' ') || "'none'") +
      "; style-src 'unsafe-inline'; font-src 'none'; form-action 'none'; base-uri 'none'";
    parsed.head.prepend(policy);
    const style = parsed.createElement('style');
    style.textContent = 'html{overflow:auto!important;background:#fff;color:#111;color-scheme:light}body{min-height:340px!important;height:auto!important;margin:16px;font-family:"Times New Roman",serif;font-size:16px;line-height:1.35;outline:0;overflow-wrap:break-word}img{max-width:100%}' +
      'html[data-sf-editor-theme="dark"],html[data-sf-editor-theme="dark"] body{background:#3c3c3c!important;color:#f0f0f0!important;color-scheme:dark}' +
      'html[data-sf-editor-theme="dark"] a[href]{color:#4fc1ff}';
    parsed.head.append(style);
    parsed.documentElement.setAttribute('data-sf-editor-theme', document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
    parsed.body.setAttribute('contenteditable', 'true');
    parsed.body.setAttribute('role', 'textbox');
    parsed.body.setAttribute('aria-label', 'Email template body');
    parsed.body.setAttribute('aria-multiline', 'true');
    if (frameVersion !== null) parsed.documentElement.setAttribute('data-sf-frame-version', String(frameVersion));
    return '<!doctype html>\n' + parsed.documentElement.outerHTML;
  }

  function replaceVisualBody(source, body) {
    const parts = source.match(/^([\s\S]*?<body\b[^>]*>)[\s\S]*(<\/body\s*>[\s\S]*)$/i);
    return parts ? parts[1] + body + parts[2] : body;
  }

  class TemplateEditor {
    constructor(root) {
      this.root = root;
      this.rows = [];
      this.current = null;
      this.isNew = false;
      this.returnId = '';
      this.operation = '';
      this.rawBody = '';
      this.mode = 'formatted';
      this.loaded = false;
      this.loading = false;
      this.saving = false;
      this.dirty = false;
      this.range = null;
      this.frameVersion = 0;
      this.frameReady = false;
      this.select = byId('sfTemplateSelect');
      this.name = byId('sfTemplateName');
      this.subject = byId('sfTemplateSubject');
      this.source = byId('sfTemplateBodyHtml');
      this.frame = byId('sfTemplateBodyFrame');
      this.status = byId('sfTemplateStatus');
      this.saveButton = byId('sfTemplateSave');
      this.reloadButton = byId('sfTemplateReload');
      this.addButton = byId('sfTemplateAdd');
      this.duplicateButton = byId('sfTemplateDuplicate');
      this.removeButton = byId('sfTemplateRemove');
      this.cancelButton = byId('sfTemplateCancel');
      this.viewToggle = byId('sfTemplateViewToggle');
      this.dialog = byId('sfTemplateAssetDialog');
      this.bind();
      this.themeObserver = new MutationObserver(() => this.syncVisualTheme());
      this.themeObserver.observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
      this.renderVisual();
    }

    bind() {
      this.select.addEventListener('change', () => {
        const selected = this.select.value;
        if (!this.discardAllowed()) {
          this.select.value = this.isNew ? '' : (this.current ? this.current.id : '');
          return;
        }
        this.choose(selected);
        this.populateOptions(selected);
      });
      for (const input of [this.name, this.subject]) input.addEventListener('input', () => this.changed());
      this.source.addEventListener('input', () => {
        this.rawBody = this.source.value;
        this.changed();
      });
      this.saveButton.addEventListener('click', () => this.save());
      this.reloadButton.addEventListener('click', () => this.load(true));
      this.addButton.addEventListener('click', () => this.add());
      this.duplicateButton.addEventListener('click', () => this.duplicate());
      this.removeButton.addEventListener('click', () => this.remove());
      this.cancelButton.addEventListener('click', () => this.cancelNew());
      this.viewToggle.addEventListener('click', () => this.setMode(this.mode === 'html' ? 'formatted' : 'html'));
      const toolbar = byId('sfTemplateToolbar');
      toolbar.addEventListener('mousedown', (event) => {
        this.rememberSelection();
        if (event.target.closest('button')) event.preventDefault();
      });
      for (const button of toolbar.querySelectorAll('[data-sf-command]')) {
        button.addEventListener('click', () => this.command(button.dataset.sfCommand));
      }
      byId('sfTemplateFont').addEventListener('change', (event) => {
        if (event.target.value) this.command('fontName', event.target.value);
      });
      byId('sfTemplateSize').addEventListener('change', (event) => {
        if (event.target.value) this.command('fontSize', event.target.value);
      });
      byId('sfTemplateColor').addEventListener('input', (event) => {
        this.command('foreColor', event.target.value);
        event.target.parentElement.style.borderBottomColor = event.target.value;
      });
      byId('sfTemplateLink').addEventListener('click', () => this.openAsset('link'));
      byId('sfTemplateImage').addEventListener('click', () => this.openAsset('image'));
      byId('sfTemplateAssetCancel').addEventListener('click', () => this.dialog.close());
      byId('sfTemplateAssetForm').addEventListener('submit', (event) => {
        event.preventDefault();
        this.insertAsset();
      });
      this.root.addEventListener('keydown', (event) => this.saveShortcut(event));
      window.addEventListener('beforeunload', (event) => {
        if (this.dirty || this.saving) { event.preventDefault(); event.returnValue = ''; }
      });
    }

    saveShortcut(event) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        this.save();
      }
    }

    message(text, state = '') {
      this.status.textContent = text;
      this.status.dataset.state = state;
      if (typeof window.setSettingsSaveState === 'function') {
        window.setSettingsSaveState(text, state === 'error' ? 'error' : (state === 'saving' ? 'saving' : ''));
      }
    }

    controls() {
      const busy = this.loading || this.saving;
      const unavailable = busy || !this.current;
      this.select.disabled = busy || this.rows.length === 0;
      this.reloadButton.disabled = busy;
      this.addButton.disabled = busy || !this.loaded;
      this.duplicateButton.disabled = unavailable || this.isNew;
      this.removeButton.disabled = unavailable || this.isNew;
      this.removeButton.textContent = this.saving && this.operation === 'remove' ? 'Removing…' : 'Remove';
      this.cancelButton.hidden = !this.isNew;
      this.cancelButton.disabled = busy;
      for (const input of [this.name, this.subject, this.source]) input.disabled = unavailable;
      this.viewToggle.disabled = unavailable;
      this.viewToggle.setAttribute('aria-pressed', String(this.mode === 'html'));
      this.viewToggle.title = this.mode === 'html' ? 'Show formatted view' : 'Show HTML source';
      for (const control of this.root.querySelectorAll('[data-sf-format]')) {
        control.disabled = unavailable || this.mode === 'html' || !this.frameReady;
      }
      if (this.frameReady) this.frame.contentDocument.body.contentEditable = String(!unavailable);
      this.saveButton.disabled = unavailable || !this.dirty;
      this.saveButton.textContent = this.saving && this.operation === 'save' ? 'Saving…' : (this.isNew ? 'Create template' : 'Save template');
    }

    draft() {
      return {name: this.name.value, subject: this.subject.value, body_html: this.rawBody};
    }

    changed() {
      this.dirty = !!this.current && (this.isNew || !sameDraft(this.draft(), this.current));
      this.message(this.dirty ? 'Unsaved template changes' : 'No unsaved template changes', this.dirty ? 'dirty' : '');
      this.controls();
    }

    discardAllowed() {
      return !this.dirty || window.confirm('Discard unsaved changes to this template?');
    }

    populateOptions(selectedId) {
      this.select.replaceChildren();
      for (const row of this.rows) {
        const option = document.createElement('option');
        option.value = row.id;
        option.textContent = row.name;
        this.select.append(option);
      }
      if (this.isNew) this.select.append(new Option('New template (unsaved)', ''));
      else if (!this.rows.length) this.select.append(new Option('No saved templates', ''));
      this.select.value = this.isNew ? '' : (this.rows.some((row) => row.id === selectedId) ? selectedId : (this.rows[0] ? this.rows[0].id : ''));
      byId('sfTemplateCount').textContent = this.rows.length + (this.rows.length === 1 ? ' saved template' : ' saved templates');
    }

    choose(id) {
      this.isNew = false;
      this.current = this.rows.find((row) => row.id === id) || null;
      this.showCurrent();
    }

    showCurrent() {
      this.name.value = this.current ? this.current.name : '';
      this.subject.value = this.current ? this.current.subject : '';
      this.rawBody = this.current ? this.current.body_html : '';
      this.source.value = this.rawBody;
      this.dirty = false;
      this.range = null;
      byId('sfTemplateFont').value = '';
      byId('sfTemplateSize').value = '';
      byId('sfTemplateColor').value = '#000000';
      byId('sfTemplateColor').parentElement.style.borderBottomColor = '#000000';
      this.renderVisual();
      this.message(this.current ? 'No unsaved template changes' : 'No saved templates. Select Add to create one.');
      this.controls();
    }

    uniqueName(base, copy = false) {
      const names = new Set(this.rows.map((row) => row.name.trim().toLocaleLowerCase()));
      for (let number = 1; ; number += 1) {
        const suffix = copy ? (number === 1 ? ' (copy)' : ' (copy ' + number + ')') : (number === 1 ? '' : ' (' + number + ')');
        const name = base.slice(0, 200 - suffix.length).replace(/[\uD800-\uDBFF]$/, '').trimEnd() + suffix;
        if (!names.has(name.trim().toLocaleLowerCase())) return name;
      }
    }

    beginNew(fields) {
      this.returnId = (this.current && this.current.id) || this.returnId || (this.rows[0] && this.rows[0].id) || '';
      this.current = {...fields, id: null, revision: null};
      this.isNew = true;
      this.showCurrent();
      this.dirty = true;
      this.populateOptions('');
      this.message('New template draft. Select Create template to save it.', 'dirty');
      this.controls();
      this.name.focus();
      this.name.select();
    }

    add() {
      if (!this.loaded || this.loading || this.saving || !this.discardAllowed()) return;
      this.beginNew({name: this.uniqueName('[AUTO] New template'), subject: 'Order #[ORDER-NUMBER]', body_html: '<p>Hello,</p>\n<p><br></p>'});
    }

    duplicate() {
      if (!this.current || this.isNew || this.loading || this.saving) return;
      const fields = this.draft();
      this.beginNew({...fields, name: this.uniqueName(fields.name.trim() || this.current.name, true)});
    }

    cancelNew() {
      if (!this.isNew || this.loading || this.saving || !this.discardAllowed()) return;
      const selected = this.rows.some((row) => row.id === this.returnId) ? this.returnId : (this.rows[0] ? this.rows[0].id : '');
      this.choose(selected);
      this.populateOptions(selected);
    }

    async remove() {
      if (!this.current || this.isNew || this.loading || this.saving) return;
      const template = this.current;
      const warning = 'Remove "' + template.name + '" from this computer\'s library?\n\nAutomations that use it will stop until it is replaced.' +
        (this.dirty ? '\nYour unsaved edits will also be discarded.' : '');
      if (!window.confirm(warning)) return;
      this.saving = true;
      this.operation = 'remove';
      this.controls();
      this.message('Removing template…', 'saving');
      try {
        const data = await window.api('/api/salesforce-templates/' + encodeURIComponent(template.id), {
          method: 'DELETE', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({revision: template.revision}),
        });
        if (!data.success) throw new Error(data.message || 'Template could not be removed.');
        const index = this.rows.findIndex((row) => row.id === template.id);
        this.rows = this.rows.filter((row) => row.id !== template.id);
        const next = this.rows[Math.min(index, this.rows.length - 1)];
        this.choose(next ? next.id : '');
        this.populateOptions(next ? next.id : '');
        this.message(data.message || 'Template removed.', 'saved');
      } catch (error) {
        this.message(error.message || 'Template could not be removed.', 'error');
      } finally {
        this.saving = false;
        this.operation = '';
        this.controls();
      }
    }

    async load(force = false) {
      if (this.loading || this.saving || (this.loaded && !force)) return false;
      if (force && !this.discardAllowed()) return false;
      this.loading = true;
      this.controls();
      this.message('Loading templates…', 'saving');
      try {
        const data = await window.api('/api/salesforce-templates');
        if (!data.success || !Array.isArray(data.templates)) throw new Error(data.message || 'Templates could not be loaded.');
        const previous = (this.current && this.current.id) || (this.isNew ? this.returnId : '');
        this.rows = data.templates.sort((a, b) => a.name.localeCompare(b.name));
        this.isNew = false;
        this.populateOptions(previous);
        this.choose(this.select.value);
        this.loaded = true;
        return true;
      } catch (error) {
        this.message(error.message || 'Templates could not be loaded.', 'error');
        return false;
      } finally {
        this.loading = false;
        this.controls();
      }
    }

    setMode(mode) {
      if (!this.current || this.saving || mode === this.mode) return;
      this.mode = mode;
      this.source.hidden = mode !== 'html';
      this.frame.hidden = mode === 'html';
      this.source.value = this.rawBody;
      if (mode === 'formatted') this.renderVisual();
      this.controls();
    }

    syncVisualTheme() {
      if (!this.frameReady) return;
      // Keep theme styling in the frame head/root, outside the saved body markup.
      this.frame.contentDocument.documentElement.setAttribute('data-sf-editor-theme',
        document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
    }

    renderVisual() {
      const version = ++this.frameVersion;
      this.frameReady = false;
      this.range = null;
      this.controls();
      this.frame.onload = () => {
        if (version !== this.frameVersion) return;
        const doc = this.frame.contentDocument;
        if (!doc || !doc.body || doc.documentElement.getAttribute('data-sf-frame-version') !== String(version)) return;
        this.frameReady = true;
        this.syncVisualTheme();
        doc.execCommand('styleWithCSS', false, true);
        doc.addEventListener('selectionchange', () => this.rememberSelection());
        doc.addEventListener('input', () => this.visualChanged());
        doc.addEventListener('keydown', (event) => this.saveShortcut(event));
        doc.addEventListener('click', (event) => {
          if (event.target.closest('a')) event.preventDefault();
        });
        doc.addEventListener('drop', (event) => event.preventDefault());
        doc.addEventListener('paste', (event) => {
          if (!this.current || this.saving || !event.clipboardData) return;
          event.preventDefault();
          const html = event.clipboardData.getData('text/html');
          if (html) {
            const safe = new DOMParser().parseFromString(previewDocument(html), 'text/html');
            doc.execCommand('insertHTML', false, safe.body.innerHTML);
          } else doc.execCommand('insertText', false, event.clipboardData.getData('text/plain'));
          this.visualChanged();
        });
        this.controls();
      };
      this.frame.srcdoc = previewDocument(this.rawBody, version);
    }

    visualChanged() {
      if (!this.frameReady || !this.current || this.saving) return;
      this.rawBody = replaceVisualBody(this.rawBody, this.frame.contentDocument.body.innerHTML);
      this.source.value = this.rawBody;
      this.rememberSelection();
      this.changed();
    }

    rememberSelection() {
      if (!this.frameReady) return;
      const doc = this.frame.contentDocument;
      const selection = doc.getSelection();
      if (selection && selection.rangeCount && doc.body.contains(selection.getRangeAt(0).commonAncestorContainer)) {
        this.range = selection.getRangeAt(0).cloneRange();
      }
      for (const button of this.root.querySelectorAll('[data-sf-command][aria-pressed]')) {
        button.setAttribute('aria-pressed', String(doc.queryCommandState(button.dataset.sfCommand)));
      }
    }

    restoreSelection() {
      const doc = this.frame.contentDocument;
      doc.body.focus();
      const selection = doc.getSelection();
      selection.removeAllRanges();
      if (this.range && doc.body.contains(this.range.commonAncestorContainer)) selection.addRange(this.range);
      else {
        const range = doc.createRange();
        range.selectNodeContents(doc.body);
        range.collapse(false);
        selection.addRange(range);
      }
      return doc;
    }

    command(name, value = null) {
      if (!this.current || !this.frameReady || this.mode === 'html' || this.saving) return;
      const doc = this.restoreSelection();
      doc.execCommand(name, false, value);
      this.visualChanged();
    }

    openAsset(kind) {
      if (!this.frameReady || this.mode === 'html' || this.saving) return;
      this.rememberSelection();
      this.assetKind = kind;
      this.assetSelection = this.range ? this.range.toString() : '';
      byId('sfTemplateAssetHeading').textContent = kind === 'image' ? 'Insert image' : 'Insert link';
      byId('sfTemplateAssetTextLabel').textContent = kind === 'image' ? 'Alternative text' : 'Link text';
      byId('sfTemplateAssetUrl').value = '';
      byId('sfTemplateAssetText').value = kind === 'link' ? this.assetSelection : '';
      byId('sfTemplateAssetError').textContent = '';
      this.dialog.showModal();
      byId('sfTemplateAssetUrl').focus();
    }

    insertAsset() {
      const url = byId('sfTemplateAssetUrl').value.trim();
      const text = byId('sfTemplateAssetText').value;
      const image = this.assetKind === 'image';
      if (!allowedUrl(url, image)) {
        byId('sfTemplateAssetError').textContent = image ? 'Enter a public HTTPS image URL.' : 'Enter an http, https, mailto, or tel URL, or a template placeholder.';
        return;
      }
      this.dialog.close();
      const doc = this.restoreSelection();
      if (!image && this.assetSelection && text === this.assetSelection) doc.execCommand('createLink', false, url);
      else {
        const node = doc.createElement(image ? 'img' : 'a');
        if (image) {
          node.setAttribute('src', url);
          node.setAttribute('alt', text);
          node.style.maxWidth = '100%';
        } else {
          node.setAttribute('href', url);
          node.textContent = text || url;
        }
        doc.execCommand('insertHTML', false, node.outerHTML);
      }
      this.visualChanged();
      // A newly inserted host needs a fresh preview CSP. Rebuild only after
      // inserting an image, retaining the source captured from the actual edit.
      if (image) this.renderVisual();
    }

    async save() {
      if (!this.current || !this.dirty || this.loading || this.saving) return;
      if (!this.name.value.trim() || !this.subject.value.trim() || !this.rawBody.trim()) {
        this.message('Template name, title, and body are required.', 'error');
        return;
      }
      this.saving = true;
      this.operation = 'save';
      this.controls();
      this.message('Saving template…', 'saving');
      const id = this.current.id;
      const creating = this.isNew;
      try {
        const data = await window.api('/api/salesforce-templates' + (creating ? '' : '/' + encodeURIComponent(id)), {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({...this.draft(), revision: this.current.revision}),
        });
        if (!data.success || !data.template) throw new Error(data.message || 'Template could not be saved.');
        this.current = data.template;
        this.isNew = false;
        this.rows = (creating ? [...this.rows, this.current] : this.rows.map((row) => row.id === id ? this.current : row))
          .sort((a, b) => a.name.localeCompare(b.name));
        this.populateOptions(this.current.id);
        this.dirty = false;
        this.message(data.message || 'Template saved.', 'saved');
      } catch (error) {
        this.message(error.message || 'Template could not be saved. Your draft is still open.', 'error');
      } finally {
        this.saving = false;
        this.operation = '';
        this.controls();
      }
    }
  }

  function init() {
    const root = byId('sfTemplateEditor');
    if (!root) return;
    const editor = new TemplateEditor(root);
    window.SalesforceTemplateEditor = {
      open: () => editor.load(),
      reload: () => editor.load(true),
      hasUnsavedChanges: () => editor.dirty,
    };
    if (root.closest('[data-settings-section]').classList.contains('active')) editor.load();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
