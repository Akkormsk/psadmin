(function (root) {
  'use strict';
  function escape(text) {
    return String(text).replace(/[&<>"']/g, function (char) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char];
    });
  }
  function cells(text) {
    const line = text.trim(), result = [];
    let value = '', delimiter = -1;
    for (let index = 0; index < line.length; index++) {
      const char = line[index];
      if (char === '\\' && ['|', '\\'].includes(line[index + 1])) { value += line[++index]; }
      else if (char === '|') { result.push(value.trim()); value = ''; delimiter = index; }
      else value += char;
    }
    if (delimiter < 0) return null;
    result.push(value.trim());
    if (line.startsWith('|')) result.shift();
    if (delimiter === line.length - 1) result.pop();
    return result;
  }
  function render(text) {
    const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n'), output = [], plain = [];
    let fence = null;
    function flush() {
      const text = plain.join('\n').replace(/^\n+|\n+$/g, '');
      if (text) output.push('<p>' + escape(text) + '</p>');
      plain.length = 0;
    }
    for (let index = 0; index < lines.length; index++) {
      const marker = lines[index].match(/^\s*(`{3,}|~{3,})/);
      if (marker) {
        if (!fence) fence = marker[1];
        else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = null;
        plain.push(lines[index]);
        continue;
      }
      const headers = !fence && cells(lines[index]);
      const separator = headers && index + 1 < lines.length && cells(lines[index + 1]);
      if (!separator || headers.length !== separator.length || !separator.every(cell => /^:?-{3,}:?$/.test(cell))) {
        plain.push(lines[index]);
        continue;
      }
      const rows = [];
      let end = index + 2, valid = true;
      while (end < lines.length) {
        const row = cells(lines[end]);
        if (!row) break;
        if (row.length !== headers.length) valid = false;
        rows.push(row);
        end++;
      }
      if (!valid || !rows.length) {
        plain.push(...lines.slice(index, end));
      } else {
        flush();
        output.push('<div class="assistant-records">' + rows.map(row =>
          '<dl class="assistant-record">' + row.map((value, column) =>
            '<div><dt>' + escape(headers[column]) + '</dt><dd>' + escape(value) + '</dd></div>'
          ).join('') + '</dl>'
        ).join('') + '</div>');
      }
      index = end - 1;
    }
    flush();
    return output.join('');
  }
  const api = {render};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.psAssistantFormat = api;
})(typeof window !== 'undefined' ? window : globalThis);
