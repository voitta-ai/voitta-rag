/**
 * Project management: create, delete, switch projects.
 */
import { state } from './state.js';
import { showToast, escapeHtml } from './ui.js';
import { refreshFileList } from './file-ops.js';
import { loadItemDetails } from './sidebar.js';

export async function onProjectChange(value) {
    const select = document.getElementById('project-select');
    if (value === '__manage__') {
        select.value = String(state.currentProjectId);
        openProjectModal();
        return;
    }

    try {
        const response = await fetch(`/api/projects/${value}/select`, { method: 'PUT' });
        if (!response.ok) throw new Error('Failed to switch project');
        const data = await response.json();
        state.currentProjectId = data.active_project_id;
        refreshFileList();
        const targetPath = state.selectedPath || state.currentPath;
        if (targetPath) loadItemDetails(targetPath, state.selectedIsDir);
    } catch (error) {
        showToast(error.message, 'error');
        select.value = String(state.currentProjectId);
    }
}

export function openProjectModal() {
    document.getElementById('project-modal').classList.add('active');
    loadProjectList();
}

export function closeProjectModal() {
    document.getElementById('project-modal').classList.remove('active');
}

async function loadProjectList() {
    try {
        const response = await fetch('/api/projects/');
        if (!response.ok) throw new Error('Failed to load projects');
        const data = await response.json();

        const list = document.getElementById('project-list');
        list.innerHTML = '';
        for (const project of data.projects) {
            const item = document.createElement('div');
            item.className = 'project-item';
            item.innerHTML = `
                <span class="project-item-name">${escapeHtml(project.name)}${project.is_default ? ' <span class="project-default-badge">default</span>' : ''}</span>
                ${!project.is_default ? `<button class="btn btn-text btn-sm project-delete-btn" onclick="deleteProject(${project.id}, '${escapeHtml(project.name)}')">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="btn-icon"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                </button>` : ''}
            `;
            list.appendChild(item);
        }
    } catch (error) {
        showToast(error.message, 'error');
    }
}

export async function createProject(event) {
    event.preventDefault();
    const input = document.getElementById('new-project-name');
    const name = input.value.trim();
    if (!name) return;

    try {
        const response = await fetch('/api/projects/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create project');
        }

        const project = await response.json();
        input.value = '';

        const select = document.getElementById('project-select');
        const manageOpt = select.querySelector('option[value="__manage__"]');
        const opt = document.createElement('option');
        opt.value = project.id;
        opt.textContent = project.name;
        select.insertBefore(opt, manageOpt);

        showToast(`Created project: ${name}`, 'success');
        loadProjectList();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

export async function deleteProject(id, name) {
    if (!confirm(`Delete project "${name}"?`)) return;

    try {
        const response = await fetch(`/api/projects/${id}`, { method: 'DELETE' });
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to delete project');
        }

        const data = await response.json();

        const select = document.getElementById('project-select');
        const opt = select.querySelector(`option[value="${id}"]`);
        if (opt) opt.remove();

        if (state.currentProjectId === id) {
            state.currentProjectId = data.active_project_id;
            select.value = String(state.currentProjectId);
            refreshFileList();
            const targetPath = state.selectedPath || state.currentPath;
            if (targetPath) loadItemDetails(targetPath, state.selectedIsDir);
        }

        showToast(`Deleted project: ${name}`, 'success');
        loadProjectList();
    } catch (error) {
        showToast(error.message, 'error');
    }
}
