import React, { useEffect, useRef } from 'react';
/** Native dialog supplies focus containment, Escape and return of focus. */
export function Dialog({ open, onClose, title, className = '', children }: { open: boolean; onClose: () => void; title: string; className?: string; children: React.ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    const previous = document.activeElement as HTMLElement | null;
    if (open) dialog.showModal(); else if (dialog.open) dialog.close();
    return () => { if (dialog.open) dialog.close(); previous?.focus(); };
  }, [open]);
  return <dialog ref={ref} aria-label={title} className={`app-dialog ${className}`} onCancel={e => { e.preventDefault(); onClose(); }} onClick={e => { if (e.target === e.currentTarget) onClose(); }}><div className="dialog-content">{open ? children : null}</div></dialog>;
}
