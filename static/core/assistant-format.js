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
        const columns = headers.map((_, column) => column);
        const shared = rows.length > 1 ? columns.filter(column =>
          rows[0][column] !== '' && rows.every(row => row[column] === rows[0][column])
        ) : [];
        // Even identical records must remain visible as separate records.
        if (shared.length === headers.length) shared.shift();
        const individual = columns.filter(column => !shared.includes(column));
        function pair(row, column) {
          return '<div><dt>' + escape(headers[column]) + '</dt><dd>' + escape(row[column]) + '</dd></div>';
        }
        const common = shared.length ? '<div class="assistant-records__common"><span>Для всех:</span><dl>' +
          shared.map(column => pair(rows[0], column)).join('') + '</dl></div>' : '';
        const summary = individual.length <= 3;
        const heading = summary ? '<div class="assistant-records__columns" aria-hidden="true">' +
          individual.map(column => escape(headers[column])).join(' · ') + '</div>' : '';
        output.push('<div class="assistant-records"' + (summary ? ' data-layout="summary"' : '') + '>' + common + heading + rows.map(row =>
          '<dl class="assistant-record">' + individual.map(column => pair(row, column)).join('') + '</dl>'
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
