/* Render only text and fenced code; never interpret prompt HTML. */
(() => {
  'use strict';
  const tokenPattern = /(\/\/[^\n]*|\/\*[\s\S]*?\*\/)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(^[ \t]*#[^\n]*)|\b(alignas|auto|bool|break|case|catch|char|class|const|constexpr|continue|default|delete|do|double|else|enum|false|float|for|if|int|long|namespace|new|nullptr|private|protected|public|return|short|signed|sizeof|static|std|struct|switch|template|this|throw|true|try|typedef|typename|union|unsigned|using|virtual|void|volatile|while)\b|\b(0[xX][\da-fA-F]+|\d+(?:\.\d+)?)\b/gm;
  function highlight(code, node) {
    let end = 0;
    for (const match of code.matchAll(tokenPattern)) {
      node.append(document.createTextNode(code.slice(end, match.index)));
      const span = document.createElement('span');
      span.className = 'cpp-' + (match[1] ? 'comment' : match[2] ? 'string' : match[3] ? 'directive' : match[4] ? 'keyword' : 'number');
      span.textContent = match[0];
      node.append(span);
      end = match.index + match[0].length;
    }
    node.append(document.createTextNode(code.slice(end)));
  }
  function render(source, target) {
    target.replaceChildren();
    const lines = source.replace(/\r\n?/g, '\n').split('\n');
    let prose = [], code = [], fence = null;
    function textBlock() {
      if (!prose.length) return;
      const div = document.createElement('div');
      div.className = 'prompt-prose'; div.textContent = prose.join('\n');
      target.append(div); prose = [];
    }
    function codeBlock() {
      const pre = document.createElement('pre'), content = document.createElement('code');
      pre.className = 'prompt-code';
      highlight(code.join('\n'), content); pre.append(content); target.append(pre); code = [];
    }
    for (const line of lines) {
      if (fence) {
        if (new RegExp('^ {0,3}`{' + fence.length + ',}\\s*$').test(line)) {
          codeBlock(); fence = null;
        } else code.push(line);
      } else {
        const opening = line.match(/^ {0,3}(`{3,})\s*(?:cpp|c\+\+|cxx|cc|c)?\s*$/i);
        if (opening) { textBlock(); fence = opening[1]; }
        else prose.push(line);
      }
    }
    if (fence) codeBlock();
    textBlock();
  }
  document.querySelectorAll('[data-question-prompt]').forEach(node => render(node.textContent, node));
  const input = document.getElementById('questionPrompt');
  const preview = document.getElementById('promptPreview');
  if (input && preview) {
    const update = () => render(input.value, preview);
    input.addEventListener('input', update);
    document.getElementById('insertCppCode').addEventListener('click', () => {
      const start = input.selectionStart, end = input.selectionEnd;
      const selection = input.value.slice(start, end) || '// Paste your C++ code here';
      const before = start && input.value[start - 1] !== '\n' ? '\n' : '';
      // Use a longer fence when selected code itself contains backticks.
      const runs = selection.match(/`+/g) || [];
      const fence = '`'.repeat(Math.max(3, ...runs.map(run => run.length + 1)));
      const prefix = before + fence + 'cpp\n';
      input.setRangeText(prefix + selection + '\n' + fence + '\n', start, end, 'end');
      input.focus(); input.setSelectionRange(start + prefix.length, start + prefix.length + selection.length);
      update();
    });
    update();
  }
})();
