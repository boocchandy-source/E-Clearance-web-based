// TCC eClearance — global UI helpers (vanilla JS)
document.addEventListener('DOMContentLoaded', () => {
  // auto-dismiss flashes
  setTimeout(() => {
    document.querySelectorAll('.flash').forEach(f => {
      f.style.transition = 'opacity .5s ease'; f.style.opacity = '0';
      setTimeout(() => f.remove(), 550);
    });
  }, 4200);

  // close modal on backdrop click (outer dialog click)
  document.querySelectorAll('dialog.modal').forEach(d => {
    d.addEventListener('click', e => { if (e.target === d) d.close(); });
  });
});
