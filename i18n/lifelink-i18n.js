/*
 * LifeLink regional-language support (i18next).
 *
 * The app renders most of its text from JS template strings, so instead of
 * threading i18next.t() through every render function this module translates
 * the DOM itself: every text node and placeholder/title attribute whose
 * English text has a row in translations.js is swapped for the active
 * language, and a MutationObserver does the same for anything the app renders
 * later. The English source is remembered per node, so switching languages
 * (including back to English) always re-translates from the original.
 *
 * Skipped: Material Symbols icon ligatures, <script>/<style>/<textarea>, and
 * anything inside [data-no-i18n].
 */
(function () {
  'use strict';

  if (!window.i18next || !window.LIFELINK_TRANSLATIONS) {
    console.warn('[LifeLink i18n] i18next or translations missing; UI stays in English.');
    return;
  }

  const LANGS = window.LIFELINK_LANGUAGES;
  const COLUMNS = window.LIFELINK_TRANSLATION_COLUMNS;
  const STORAGE_KEY = 'lifelink_language';
  const ATTRS = ['placeholder', 'title'];
  const SKIP_SELECTOR = 'script, style, textarea, .material-symbols-outlined, [data-no-i18n]';
  const PLACEHOLDER_RE = /\{\{(\w+)\}\}/g;
  const ORIGINAL_TITLE = document.title;

  const collapse = (s) => s.replace(/\s+/g, ' ').trim();
  const normKey = (s) => collapse(s).toUpperCase();
  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  // ------------------------------------------------------------ resources
  const resources = {};
  COLUMNS.forEach((code) => { resources[code] = { translation: {} }; });
  const patterns = [];

  window.LIFELINK_TRANSLATIONS.forEach((row) => {
    const key = normKey(row[0]);
    COLUMNS.forEach((code, i) => {
      if (row[i + 1]) resources[code].translation[key] = row[i + 1];
    });
    // Placeholder names come from the original row: the key is upper-cased, the translations are not.
    const names = [...row[0].matchAll(PLACEHOLDER_RE)].map((m) => m[1]);
    if (names.length) {
      const re = new RegExp('^' + key.split(/\{\{\w+\}\}/).map(escapeRe).join('(.+?)') + '$', 'i');
      patterns.push({ key, names, re, literalLength: key.replace(PLACEHOLDER_RE, '').length });
    }
  });
  // Most specific (longest literal text) first.
  patterns.sort((a, b) => b.literalLength - a.literalLength);

  i18next.init({
    lng: 'en',
    fallbackLng: false,
    resources,
    keySeparator: false,
    nsSeparator: false,
    returnEmptyString: false,
    interpolation: { escapeValue: false }
  });

  // ------------------------------------------------------------ lookup
  // Returns the translation of `text` in the active language, or null.
  function lookup(text, depth) {
    if (i18next.language === 'en') return null;
    const collapsed = collapse(text);
    if (!collapsed || !/[A-Za-z]/.test(collapsed)) return null;

    const key = collapsed.toUpperCase();
    if (i18next.exists(key)) return i18next.t(key);

    if ((depth || 0) > 2) return null;
    for (const p of patterns) {
      const m = collapsed.match(p.re);
      if (!m) continue;
      const vars = {};
      p.names.forEach((name, i) => {
        const value = m[i + 1];
        // {{name}} placeholders hold a person's name: show it in the UI script
        const translated = name === 'name' ? transliterateName(value) : lookup(value, (depth || 0) + 1);
        vars[name] = translated == null ? value : translated;
      });
      const out = i18next.t(p.key, vars);
      // A pattern that only reshuffles untranslated values (e.g. "{{category}} ({{n}})") is a no-op.
      if (out === collapsed) continue;
      return out;
    }
    return null;
  }

  // Keeps the original leading/trailing whitespace around a translated core.
  function translateString(src) {
    const out = lookup(src);
    if (out == null) return src;
    const lead = src.match(/^\s*/)[0];
    const trail = src.match(/\s*$/)[0];
    return lead + out + trail;
  }

  // Names (patients, contacts) are stored in English and only DISPLAYED in the
  // UI script, via the offline transliterator in transliterate.js.
  function transliterateName(text) {
    const lang = i18next.language;
    if (lang === 'en' || !window.LifeLinkTranslit) return text;
    return window.LifeLinkTranslit.transliterate(text, lang);
  }

  // Text inside [data-translit]: UI fallbacks ("UNNAMED PATIENT", "None recorded")
  // still use the dictionary; anything else is a name and is transliterated.
  function translateNameText(src) {
    if (i18next.language === 'en') return src;
    return lookup(src) != null ? translateString(src) : transliterateName(src);
  }

  // ------------------------------------------------------------ DOM
  const textState = new WeakMap(); // Text -> { src, out }
  const attrState = new WeakMap(); // Element -> { [attr]: { src, out } }

  function isSkipped(el) {
    return !el || !!el.closest(SKIP_SELECTOR);
  }

  function applyText(node) {
    if (isSkipped(node.parentElement)) return;
    const prev = textState.get(node);
    // Our own output → keep the remembered English source; anything else is new app text.
    const src = prev && node.data === prev.out ? prev.src : node.data;
    const out = node.parentElement.closest('[data-translit]') ? translateNameText(src) : translateString(src);
    textState.set(node, { src, out });
    if (node.data !== out) node.data = out;
  }

  function applyAttr(el, attr) {
    if (!el.hasAttribute(attr) || isSkipped(el)) return;
    const current = el.getAttribute(attr);
    const states = attrState.get(el) || {};
    const prev = states[attr];
    const src = prev && current === prev.out ? prev.src : current;
    const out = translateString(src);
    states[attr] = { src, out };
    attrState.set(el, states);
    if (current !== out) el.setAttribute(attr, out);
  }

  function applyTree(root) {
    if (root.nodeType === Node.TEXT_NODE) { applyText(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE || isSkipped(root)) return;

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) applyText(node);

    ATTRS.forEach((attr) => {
      if (root.hasAttribute(attr)) applyAttr(root, attr);
      root.querySelectorAll('[' + attr + ']').forEach((el) => applyAttr(el, attr));
    });
  }

  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.type === 'childList') m.addedNodes.forEach(applyTree);
      else if (m.type === 'characterData') applyText(m.target);
      else if (m.type === 'attributes') applyAttr(m.target, m.attributeName);
    }
  });

  // ------------------------------------------------------------ switcher UI
  const toggle = document.getElementById('lang-toggle');
  const menu = document.getElementById('lang-menu');
  const current = document.getElementById('lang-current');

  function renderMenu() {
    if (!menu) return;
    menu.innerHTML = '';
    LANGS.forEach((lang) => {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'll-lang-option';
      btn.setAttribute('role', 'option');
      btn.setAttribute('aria-selected', String(lang.code === i18next.language));
      btn.setAttribute('lang', lang.code);
      btn.innerHTML = '<span class="ll-lang-native"></span><span class="ll-lang-code"></span>';
      btn.firstChild.textContent = lang.native;
      btn.lastChild.textContent = lang.code.toUpperCase();
      btn.addEventListener('click', () => { setLanguage(lang.code); closeMenu(); });
      li.appendChild(btn);
      menu.appendChild(li);
    });
  }

  function openMenu() {
    if (!menu) return;
    menu.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    const selected = menu.querySelector('[aria-selected="true"]');
    if (selected) selected.focus();
  }

  function closeMenu() {
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
  }

  if (toggle && menu) {
    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      menu.hidden ? openMenu() : closeMenu();
    });
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#lang-switcher')) closeMenu();
    });
    // Capture phase so ESC closes the menu instead of triggering the app's global "back".
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !menu.hidden) {
        e.preventDefault();
        e.stopImmediatePropagation();
        closeMenu();
        toggle.focus();
      }
    }, true);
  }

  // ------------------------------------------------------------ name input previews
  // <input data-translit-input="previewId">: the value stays in English (that is what
  // gets saved); the preview line under it shows the name in the UI script.
  function updateNamePreview(input) {
    const preview = document.getElementById(input.getAttribute('data-translit-input'));
    if (!preview) return;
    const value = input.value.trim();
    const show = i18next.language !== 'en' && /[A-Za-z]/.test(value);
    preview.hidden = !show;
    preview.textContent = show ? transliterateName(value) : '';
  }
  function refreshNamePreviews() {
    document.querySelectorAll('[data-translit-input]').forEach(updateNamePreview);
  }
  document.addEventListener('input', (e) => {
    if (e.target.matches && e.target.matches('[data-translit-input]')) updateNamePreview(e.target);
  });

  // ------------------------------------------------------------ public API
  function setLanguage(code) {
    if (!LANGS.some((l) => l.code === code)) code = 'en';
    i18next.changeLanguage(code);
    document.documentElement.lang = code;
    document.documentElement.classList.toggle('ll-indic', code !== 'en');
    try { localStorage.setItem(STORAGE_KEY, code); } catch (e) { /* storage unavailable */ }

    const lang = LANGS.find((l) => l.code === code);
    if (current) current.textContent = lang.short;
    renderMenu();

    document.title = translateString(ORIGINAL_TITLE);
    if (typeof window.updateLiveClock === 'function') window.updateLiveClock(); // localized date

    observer.disconnect();
    applyTree(document.body);
    observer.observe(document.body, {
      childList: true,
      characterData: true,
      subtree: true,
      attributes: true,
      attributeFilter: ATTRS
    });
    refreshNamePreviews();
  }

  window.LifeLinkI18n = {
    setLanguage,
    translate: (text) => translateString(text),
    transliterate: (text) => transliterateName(text),
    refreshNamePreviews,
    get language() { return i18next.language; }
  };

  let saved = 'en';
  try { saved = localStorage.getItem(STORAGE_KEY) || 'en'; } catch (e) { /* storage unavailable */ }
  setLanguage(saved);
})();
