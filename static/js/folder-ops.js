/**
 * Folder operations: create and delete folders.
 */
import { state } from './state.js';
import { showToast } from './ui.js';

function _isAnamnesisPath(p) {
    return p === 'Anamnesis' || (p && p.startsWith('Anamnesis/'));
}

export function openCreateFolderModal() {
    document.getElementById('create-folder-modal').classList.add('active');
    document.getElementById('folder-name').focus();
}

export function closeCreateFolderModal() {
    document.getElementById('create-folder-modal').classList.remove('active');
    document.getElementById('folder-name').value = '';
}

export async function createFolder(event) {
    event.preventDefault();

    const name = document.getElementById('folder-name').value.trim();
    if (!name) return;

    try {
        const response = await fetch('/api/folders', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name, path: state.currentPath }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create folder');
        }

        showToast(`Created folder: ${name}`, 'success');
        closeCreateFolderModal();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

export function openDeleteFolderModal() {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        return;
    }
    if (_isAnamnesisPath(targetPath)) {
        showToast('Anamnesis folder is read-only', 'error');
        return;
    }
    state.deleteFolderTargetPath = targetPath;
    const folderName = targetPath.split('/').pop();
    document.getElementById('delete-folder-target-name').textContent = folderName;
    document.getElementById('delete-folder-confirm-name').value = '';
    document.getElementById('delete-folder-modal').classList.add('active');
    document.getElementById('delete-folder-confirm-name').focus();
}

export function closeDeleteFolderModal() {
    document.getElementById('delete-folder-modal').classList.remove('active');
    document.getElementById('delete-folder-confirm-name').value = '';
    state.deleteFolderTargetPath = null;
}

export async function confirmDeleteFolder(event) {
    event.preventDefault();

    const confirmName = document.getElementById('delete-folder-confirm-name').value.trim();
    const folderName = state.deleteFolderTargetPath.split('/').pop();

    if (confirmName.toLowerCase() !== folderName.toLowerCase()) {
        showToast('Folder name does not match', 'error');
        return;
    }

    const btn = document.getElementById('btn-confirm-delete');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-inline"></span> Deleting...';

    state.deletingFolders.add(state.deleteFolderTargetPath);

    try {
        const response = await fetch(`/api/folders/${encodeURIComponent(state.deleteFolderTargetPath)}`, {
            method: 'DELETE',
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to delete folder');
        }

        showToast(`Deleted folder: ${folderName}`, 'success');

        const parentPath = state.deleteFolderTargetPath.split('/').slice(0, -1).join('/');
        closeDeleteFolderModal();
        window.location.href = parentPath ? `/browse/${parentPath}` : '/browse';
    } catch (error) {
        showToast(error.message, 'error');
        btn.disabled = false;
        btn.textContent = originalText;
        state.deletingFolders.delete(state.deleteFolderTargetPath);
    }
}
