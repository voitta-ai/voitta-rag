/**
 * File operations: refresh file list, upload files, sorting.
 */
import { state } from './state.js';
import { showToast } from './ui.js';

export function updateFileListFooter(fileList) {
    const footer = document.getElementById('file-list-footer');
    if (!footer) return;
    const count = (fileList || document.getElementById('file-list'))
        .querySelectorAll('.file-item').length;
    footer.textContent = `${count} item${count !== 1 ? 's' : ''}`;
}

export function refreshFileList() {
    if (state.refreshDebounceTimeout) {
        clearTimeout(state.refreshDebounceTimeout);
    }

    state.refreshDebounceTimeout = setTimeout(async () => {
        state.refreshDebounceTimeout = null;
        try {
            const url = state.currentPath
                ? `/api/browse-list/${encodeURIComponent(state.currentPath)}`
                : '/api/browse-list';
            const response = await fetch(url);
            if (!response.ok) throw new Error('Failed to fetch file list');

            const html = await response.text();
            const fileList = document.getElementById('file-list');
            if (!fileList) return;

            const prevSelectedPath = state.selectedPath;
            const prevScrollTop = fileList.scrollTop;

            fileList.innerHTML = html;
            fileList.scrollTop = prevScrollTop;

            // Re-apply current sort order
            if (state.currentSortColumn !== 'name' || !state.currentSortAsc) {
                sortFileList(state.currentSortColumn);
            }

            // Restore selection if the item still exists
            if (prevSelectedPath) {
                const item = fileList.querySelector(`.file-item[data-path="${CSS.escape(prevSelectedPath)}"]`);
                if (item) {
                    item.classList.add('selected');
                    state.selectedItem = item;
                } else {
                    state.selectedItem = null;
                    state.selectedPath = null;
                    state.selectedIsDir = false;
                }
            }

            updateFileListFooter(fileList);
        } catch (error) {
            console.error('Error refreshing file list:', error);
        }
    }, 300);
}

export async function uploadFiles(files) {
    if (!files || files.length === 0) return;

    const total = files.length;
    const modal = document.getElementById('upload-modal');
    const statusEl = document.getElementById('upload-status');
    const fillEl = document.getElementById('upload-progress-fill');
    const currentEl = document.getElementById('upload-current-file');

    // Show progress modal
    if (modal) modal.classList.add('active');
    if (fillEl) fillEl.style.width = '0%';

    const uploadErrors = [];
    for (let i = 0; i < total; i++) {
        const file = files[i];

        if (statusEl) statusEl.textContent = `${i + 1} of ${total}`;
        if (currentEl) currentEl.textContent = file.name;
        if (fillEl) fillEl.style.width = `${((i) / total) * 100}%`;

        const formData = new FormData();
        formData.append('file', file);
        formData.append('path', state.currentPath);

        try {
            const response = await fetch('/api/files/upload', {
                method: 'POST',
                body: formData,
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Upload failed');
            }
        } catch (error) {
            uploadErrors.push(file.name);
        }
    }

    // Fill to 100% before closing
    if (fillEl) fillEl.style.width = '100%';
    await new Promise(r => setTimeout(r, 200));
    if (modal) modal.classList.remove('active');

    if (uploadErrors.length > 0) {
        showToast(`Upload: ${total - uploadErrors.length} ok, ${uploadErrors.length} failed`, 'error');
    } else if (total > 1) {
        showToast(`Uploaded ${total} files`, 'success');
    }

    document.getElementById('file-upload').value = '';
    refreshFileList();
}

export function sortFileList(column) {
    if (state.currentSortColumn === column) {
        state.currentSortAsc = !state.currentSortAsc;
    } else {
        state.currentSortColumn = column;
        state.currentSortAsc = true;
    }

    // Update header indicators
    document.querySelectorAll('.sortable-header').forEach(h => {
        const arrow = h.querySelector('.sort-arrow');
        if (h.dataset.sort === column) {
            h.classList.add('active');
            arrow.textContent = state.currentSortAsc ? '\u25B2' : '\u25BC';
        } else {
            h.classList.remove('active');
            arrow.textContent = '';
        }
    });

    const list = document.getElementById('file-list');
    if (!list) return;

    const items = Array.from(list.querySelectorAll('.file-item'));
    if (items.length === 0) return;

    const indexOrder = { indexing: 0, pending: 1, indexed: 2, error: 3, disabled: 4, none: 5 };

    items.sort((a, b) => {
        const aDir = a.dataset.isDir === '1';
        const bDir = b.dataset.isDir === '1';
        if (aDir !== bDir) return aDir ? -1 : 1;

        let cmp = 0;
        switch (column) {
            case 'name':
                cmp = (a.dataset.name || '').localeCompare(b.dataset.name || '');
                break;
            case 'search': {
                const aActive = a.dataset.searchActive === 'true' ? 1 : 0;
                const bActive = b.dataset.searchActive === 'true' ? 1 : 0;
                cmp = aActive - bActive;
                break;
            }
            case 'size':
                cmp = (parseInt(a.dataset.size) || 0) - (parseInt(b.dataset.size) || 0);
                break;
            case 'index': {
                const aIdx = indexOrder[a.dataset.indexStatus] ?? 5;
                const bIdx = indexOrder[b.dataset.indexStatus] ?? 5;
                cmp = aIdx - bIdx;
                break;
            }
        }
        return state.currentSortAsc ? cmp : -cmp;
    });

    for (const item of items) {
        list.appendChild(item);
    }
}

export function updateFileListIndexStatus(path, status) {
    const fileItem = document.querySelector(`.file-item[data-path="${CSS.escape(path)}"]`);
    if (!fileItem) return;

    fileItem.dataset.indexStatus = status;
    const statusTag = fileItem.querySelector('.index-status-tag');
    if (!statusTag) return;

    statusTag.className = `index-status-tag status-${status}`;

    const statusLabels = {
        'indexed': `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="status-icon"><polyline points="20 6 9 17 4 12"></polyline></svg> Indexed`,
        'indexing': `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="status-icon spin"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"></path></svg> Indexing`,
        'pending': `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="status-icon"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg> Pending`,
        'error': `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" class="status-icon"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg> Error`,
        'none': 'Not indexed'
    };

    statusTag.innerHTML = statusLabels[status] || 'Not indexed';
}
