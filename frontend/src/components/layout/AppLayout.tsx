import React, { useEffect, useState } from 'react';
import { BookOpen, Boxes, FileText, Menu, MessageSquare, Settings2, Shield, Sparkles, Users, Wrench, X, LayoutDashboard } from 'lucide-react';
import { useAppStore } from '@/store/useAppStore';
import { Dialog } from '@/components/ui/Dialog';
import type { DependencyStatusType } from '@/types';
export type AppView = 'chat' | 'ingestion' | 'jobs' | 'tenants' | 'collections' | 'collection-config' | 'admin' | 'devhub' | 'auth';
const items: { id: AppView; label: string; icon: React.ElementType; group: string }[] = [
  { id: 'chat', label: 'Conversas', icon: MessageSquare, group: 'Workspace' },
  { id: 'collections', label: 'Bases de conhecimento', icon: BookOpen, group: 'Workspace' },
  { id: 'ingestion', label: 'Adicionar conhecimento', icon: Boxes, group: 'Workspace' },
  { id: 'jobs', label: 'Processamentos', icon: FileText, group: 'Workspace' },
  { id: 'auth', label: 'Acesso e credenciais', icon: Shield, group: 'Gerenciar' },
  { id: 'tenants', label: 'Organizações', icon: Users, group: 'Gerenciar' },
  { id: 'collection-config', label: 'Configurar bases', icon: Settings2, group: 'Gerenciar' },
  { id: 'admin', label: 'Administração', icon: LayoutDashboard, group: 'Gerenciar' },
  { id: 'devhub', label: 'Diagnóstico e serviços', icon: Wrench, group: 'Gerenciar' },
];
function statusColor(status: DependencyStatusType | undefined) { return status === 'ok' ? 'bg-emerald-500' : status === 'degraded' ? 'bg-amber-500' : status === 'error' ? 'bg-rose-500' : 'bg-zinc-600'; }
function statusRing(status: DependencyStatusType | undefined) { return status === 'ok' ? 'ring-emerald-500/30' : 'ring-zinc-600/30'; }
export { statusColor, statusRing };
export function AppLayout({ activeView, onChangeView, children }: { activeView: AppView; onChangeView: (view: AppView) => void; children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const { activeTenant, activeDomainId, tenantsList, systemHealth, isHealthChecking, setActiveTenant, setActiveDomain, checkSystemHealth } = useAppStore();
  useEffect(() => { checkSystemHealth(); const timer = setInterval(checkSystemHealth, 30_000); return () => clearInterval(timer); }, [checkSystemHealth]);
  const navigate = (view: AppView) => { onChangeView(view); setMobileOpen(false); };
  const navigation = <>
    <button className="brand" onClick={() => navigate('chat')} aria-label="Any Assistant — abrir conversas"><span className="brand-mark"><Sparkles size={21} /></span><span>Any Assistant<small>WORKSPACE DE CONHECIMENTO</small></span></button>
    <nav aria-label="Navegação principal">{['Workspace', 'Gerenciar'].map(group => <div className="nav-group" key={group}><p className="eyebrow">{group}</p>{items.filter(item => item.group === group).map(({ id, label, icon: Icon }) => <button key={id} aria-current={activeView === id ? 'page' : undefined} className={`nav-item ${activeView === id ? 'active' : ''}`} onClick={() => navigate(id)}><Icon size={17} /><span>{label}</span>{activeView === id && <span className="nav-dot" />}</button>)}</div>)}</nav>
    <div className="sidebar-footer"><span className="engineering-badge">B</span><div>Bolas Industries<small>Projeto de engenharia · em desenvolvimento</small></div></div>
  </>;
  return <div className="app-shell">
    <a href="#workspace-main" className="skip-link">Ir para o conteúdo</a><aside className="app-sidebar">{navigation}</aside>
    <Dialog open={mobileOpen} onClose={() => setMobileOpen(false)} title="Navegação" className="navigation-dialog"><button className="icon-button close-nav" onClick={() => setMobileOpen(false)} aria-label="Fechar navegação"><X size={20} /></button>{navigation}<label className="mobile-tenant-label">Organização ativa<select aria-label="Organização ativa na navegação" value={activeTenant} onChange={e => setActiveTenant(e.target.value)}>{tenantsList.length ? tenantsList.map(t => <option key={t.id} value={t.slug || t.id}>{t.name}</option>) : <option value={activeTenant}>{activeTenant}</option>}</select></label></Dialog>
    <div className="app-main"><header className="workspace-header"><div className="header-title"><button className="icon-button mobile-menu" aria-label="Abrir navegação" onClick={() => setMobileOpen(true)}><Menu size={20} /></button><span className="header-breadcrumb">Workspace <span>/</span></span><strong>{items.find(item => item.id === activeView)?.label}</strong></div>
      <div className="header-controls"><select aria-label="Domínio de conhecimento" value={activeDomainId} onChange={e => setActiveDomain(e.target.value)}><option value="godot_4">Godot 4</option><option value="ue5">Unreal Engine 5</option></select><select className="tenant-select" aria-label="Organização ativa" value={activeTenant} onChange={e => setActiveTenant(e.target.value)}>{tenantsList.length ? tenantsList.map(t => <option key={t.id} value={t.slug || t.id}>{t.name}</option>) : <option value={activeTenant}>{activeTenant}</option>}</select><button className="service-status" onClick={() => navigate('devhub')} title="Consultar diagnóstico dos serviços"><span className={`status-dot ${statusColor(systemHealth?.status)}`} /><span>{isHealthChecking ? 'Verificando' : systemHealth?.status === 'ok' ? 'Serviços disponíveis' : systemHealth?.status === 'degraded' ? 'Serviços limitados' : systemHealth?.status === 'error' ? 'Serviço indisponível' : 'Status desconhecido'}</span></button></div></header>
      <main id="workspace-main" tabIndex={-1} className="workspace-main">{children}</main>
    </div>
  </div>;
}
