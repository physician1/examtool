(() => {
  const form = document.getElementById('gradingForm');
  if (!form) return;
  const status = document.getElementById('gradingStatus');
  function fullMarks(input) {
    input.value = input.max;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
  }
  form.querySelectorAll('.full-marks').forEach(button => {
    button.addEventListener('click', () => {
      fullMarks(button.closest('.review-card').querySelector('input[name^="score_"]'));
      status.textContent = 'Full marks entered for this question. Click Save scores & feedback to save.';
    });
  });
  document.getElementById('fullMarksAll').addEventListener('click', () => {
    form.querySelectorAll('input[name^="score_"]').forEach(fullMarks);
    status.textContent = 'Full marks entered for all questions in this submission. Click Save scores & feedback to save.';
  });
})();
