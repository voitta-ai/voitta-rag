/**
 * Theme management: dark/light mode toggle.
 */
import { hideSpinner, showSpinner } from './ui.js';

function getPreferredTheme() {
    const stored = localStorage.getItem('voitta-theme');
    if (stored) return stored;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('voitta-theme', theme);
}

export function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    setTheme(next);
}

// Initialize theme on page load + hide spinner
document.addEventListener('DOMContentLoaded', () => {
    setTheme(getPreferredTheme());
    hideSpinner();
});

// Show spinner on page unload (navigation / reload)
window.addEventListener('beforeunload', () => {
    showSpinner();
});
