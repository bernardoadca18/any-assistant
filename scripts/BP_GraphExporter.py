"""
BP_GraphExporter.py
====================
Editor Utility Script — roda DENTRO da Unreal Engine 5 (Editor Python,
via "Execute Python Script" no menu Tools, ou registrado como Editor
Utility Widget/Menu Action).

Varre um diretório de assets Blueprint, extrai Classe Pai, Variáveis e o
fluxo de execução linearizado (percorrendo pinos Exec a partir de nós de
entrada), e exporta um <NomeDoAsset>.bp.json por Blueprint no disco.

REQUISITOS:
- Rodar dentro do Editor da UE5 (usa o módulo `unreal`, só existe lá).
- O plugin "Python Editor Script Plugin" deve estar habilitado
  (Edit > Plugins > Scripting > Python Editor Script Plugin).
- Testado com a API de Blueprint Graph exposta via `unreal.BlueprintEditorLibrary`
  e `unreal.EdGraph` / `unreal.EdGraphNode` (disponível desde UE 5.0+;
  algumas propriedades de nó variam entre versões — ver NOTA DE VERSÃO
  abaixo).

LIMITAÇÃO CONHECIDA — HONESTA, NÃO ESCONDIDA:
    Este script linear iza o fluxo de execução seguindo o pino de saída
    "then"/"exec" de cada nó a partir dos entry points. Isso reconstrói
    corretamente sequências lineares (Event → Node → Node → ...). Para nós
    de CONTROLE DE FLUXO com múltiplas saídas de execução (Branch: True/
    False; Sequence: 0/1/2...; ForEachLoop: LoopBody/Completed), o script
    NÃO tenta reconstruir uma árvore aninhada visualmente fiel — em vez
    disso, registra o nó de controle como um nó da sequência e lista TODAS
    as saídas de execução dele como ramos rotulados (branches), cada ramo
    contendo sua própria sub-sequência linear até convergir ou terminar.
    Isso é suficiente para a LLM documentadora entender "isso ramifica em
    X condições, e cada uma faz Y" — não é uma reconstrução perfeita de
    grafos com merges complexos (dois ramos que se reencontram no mesmo
    nó não são deduplicados; esse nó aparece uma vez em cada ramo que
    chega nele). Aceitável como trade-off: cobre o caso comum de gameplay
    real sem se comprometer a resolver o problema geral de "decompilar" um
    grafo arbitrário.

NOTA DE VERSÃO: nomes de propriedade em EdGraphNode/EdGraphPin podem variar
    sutilmente entre UE 5.0–5.8 (ex: `pin_type` vs `PinType`, disponibilidade
    de `get_editor_property("Pins")` vs atributo direto). Este script usa
    `get_editor_property()` deliberadamente em vez de atributos diretos,
    porque essa é a forma mais estável de acessar propriedades UPROPERTY
    através de versões da API Python da Unreal.
"""

import unreal
import json
import os


# =============================================================================
# Configuração — ajuste antes de rodar
# =============================================================================

# Caminho de conteúdo (Content Browser), não caminho de disco — ex: "/Game/Lyra/Characters"
SOURCE_ASSET_PATH = "/Game/YourProjectFolder"

# Caminho de DISCO onde os .bp.json serão salvos (fora do projeto, para o
# pipeline RAG ler depois).
OUTPUT_DIR = "D:/UE5_RAG_Export/blueprints"

# Nomes de eventos considerados "entry points" — pontos de partida válidos
# para iniciar o rastreamento do fluxo de execução. Nós K2Node_Event cujo
# nome de referência de função bate em qualquer um desses viram raízes de
# rastreio.
KNOWN_ENTRY_EVENT_NAMES = {
    "ReceiveBeginPlay", "ReceiveTick", "ReceiveEndPlay",
    "ReceiveActorBeginOverlap", "ReceiveActorEndOverlap",
    "ReceiveHit", "ReceiveDestroyed", "ReceivePossessed",
    "InputAction", "InputAxis", "InputTouch",
}

# Limite de segurança contra loops infinitos no rastreamento (ex: um loop
# de fluxo de execução mal montado apontando de volta para si mesmo).
MAX_TRACE_STEPS_PER_BRANCH = 300


# =============================================================================
# Helpers de baixo nível — acesso a propriedades de nó/pino
# =============================================================================

def _safe_get_editor_property(obj, prop_name, default=None):
    """
    Wrapper defensivo em torno de get_editor_property(). Nem toda versão
    da engine expõe toda propriedade em todo tipo de nó (ex: um
    K2Node_CallFunction não tem `EventReference`, e tentar acessá-la
    lançaria exceção em vez de simplesmente não existir). Isso evita que
    um único nó incomum derrube a exportação inteira do Blueprint.
    """
    try:
        return obj.get_editor_property(prop_name)
    except Exception:
        return default


def _get_node_pins(node: unreal.EdGraphNode):
    """Retorna a lista de pinos (unreal.EdGraphPin) de um nó, ou lista vazia."""
    pins = _safe_get_editor_property(node, "Pins", default=None)
    if pins is None:
        # Fallback: algumas builds expõem via método em vez de propriedade
        get_pins_fn = getattr(node, "get_all_pins", None)
        if callable(get_pins_fn):
            try:
                return list(get_pins_fn())
            except Exception:
                return []
        return []
    return list(pins)


def _pin_category_is_exec(pin: unreal.EdGraphPin) -> bool:
    """Um pino de execução (a "seta branca") tem PinCategory == 'exec'."""
    pin_type = _safe_get_editor_property(pin, "PinType", default=None)
    if pin_type is None:
        return False
    category = _safe_get_editor_property(pin_type, "PinCategory", default="")
    # PinCategory costuma vir como um FName; str() normaliza para comparação
    return str(category).lower() == "exec"


def _pin_direction_is_output(pin: unreal.EdGraphPin) -> bool:
    direction = _safe_get_editor_property(pin, "Direction", default=None)
    # EGPD_Output é o enum de "pino de saída"; comparação por string para
    # não depender de importar o enum exato (varia entre módulos/versões).
    return direction is not None and "OUTPUT" in str(direction).upper()


def _pin_direction_is_input(pin: unreal.EdGraphPin) -> bool:
    direction = _safe_get_editor_property(pin, "Direction", default=None)
    return direction is not None and "INPUT" in str(direction).upper()


def _get_linked_exec_targets(node: unreal.EdGraphNode) -> list:
    """
    Para um nó, retorna a lista de (rótulo_do_pino_de_saída, nó_destino)
    para cada pino de EXECUÇÃO DE SAÍDA que está conectado a algo.

    Rótulo importa para nós de controle de fluxo: um Branch tem pinos de
    saída "True" e "False"; um Sequence tem "0", "1", "2"...; um
    ForEachLoop tem "LoopBody" e "Completed". Capturar o rótulo é o que
    permite ao script sinalizar branches em vez de só "próximo nó".
    """
    targets = []
    for pin in _get_node_pins(node):
        if not _pin_category_is_exec(pin):
            continue
        if not _pin_direction_is_output(pin):
            continue

        pin_name = str(_safe_get_editor_property(pin, "PinName", default="then"))
        linked_pins = _safe_get_editor_property(pin, "LinkedTo", default=[])

        for linked_pin in linked_pins:
            owning_node = _safe_get_editor_property(linked_pin, "OwningNode", default=None)
            if owning_node is not None:
                targets.append((pin_name, owning_node))

    return targets


def _describe_node(node: unreal.EdGraphNode) -> dict:
    """
    Produz uma descrição textual mínima e estável de um nó, usada tanto
    para nós de controle quanto para nós de ação normais.
    """
    node_class = node.get_class().get_name()
    title = ""
    try:
        title = str(node.get_node_title(unreal.ENodeTitleType.FULL_TITLE))
    except Exception:
        # get_node_title pode falhar para tipos de nó exóticos; o nome da
        # classe sozinho ainda é informação utilizável.
        pass

    description = {
        "node_class": node_class,
        "title": title.strip() if title else node_class,
    }

    # Nós de chamada de função carregam o nome real da função chamada —
    # informação de maior valor para a LLM documentadora que o título
    # genérico do nó.
    function_ref = _safe_get_editor_property(node, "FunctionReference", default=None)
    if function_ref is not None:
        member_name = _safe_get_editor_property(function_ref, "MemberName", default=None)
        if member_name:
            description["function_name"] = str(member_name)

    # Nós de evento carregam o nome do evento (BeginPlay, Custom Event, etc.)
    event_ref = _safe_get_editor_property(node, "EventReference", default=None)
    if event_ref is not None:
        member_name = _safe_get_editor_property(event_ref, "MemberName", default=None)
        if member_name:
            description["event_name"] = str(member_name)

    # CustomEvent nodes carregam o nome próprio diretamente
    custom_event_name = _safe_get_editor_property(node, "CustomFunctionName", default=None)
    if custom_event_name:
        description["event_name"] = str(custom_event_name)

    return description


def _is_control_flow_node(node: unreal.EdGraphNode) -> bool:
    """
    Nós de controle de fluxo (múltiplas saídas exec nomeadas) merecem
    tratamento de "branch" em vez de sequência linear simples.
    """
    class_name = node.get_class().get_name()
    return class_name in {
        "K2Node_IfThenElse",       # Branch
        "K2Node_ExecutionSequence",  # Sequence
        "K2Node_ForEachElementInArrayRef",
        "K2Node_MacroInstance",   # ForEachLoop e outros macros são instâncias disso
        "K2Node_Select",
        "K2Node_SwitchEnum", "K2Node_SwitchString", "K2Node_SwitchInteger",
    }


# =============================================================================
# Rastreamento do fluxo de execução (linearização com branches)
# =============================================================================

def _trace_execution_flow(start_node: unreal.EdGraphNode, visited_global: set) -> list:
    """
    Percorre o grafo a partir de start_node seguindo pinos de execução de
    saída, produzindo uma lista de passos. Cada passo é ou:
      - {"type": "step", "node": {...}}                      — nó de ação simples
      - {"type": "branch", "node": {...}, "branches": {rótulo: [passos...]}}

    `visited_global` é compartilhado entre chamadas de rastreamento para
    permitir DETECTAR (não eliminar) loops: se um nó já visitado nesta
    árvore de rastreamento for encontrado de novo, o passo é registrado
    como referência de loop em vez de recursar infinitamente.
    """
    steps = []
    current_node = start_node
    local_path = set()  # nós já vistos NESTE ramo específico, para detectar loop

    for _ in range(MAX_TRACE_STEPS_PER_BRANCH):
        if current_node is None:
            break

        node_id = id(current_node)
        if node_id in local_path:
            steps.append({"type": "loop_reference", "note": "Fluxo retorna a um nó já percorrido neste ramo."})
            break
        local_path.add(node_id)

        node_desc = _describe_node(current_node)
        targets = _get_linked_exec_targets(current_node)

        if _is_control_flow_node(current_node) and len(targets) > 1:
            # Nó de controle com múltiplas saídas nomeadas — cada saída
            # vira um ramo rastreado independentemente.
            branches = {}
            for pin_label, target_node in targets:
                branches[pin_label] = _trace_execution_flow(target_node, visited_global)
            steps.append({"type": "branch", "node": node_desc, "branches": branches})
            break  # branches já cobrem a continuação; não seguir linear depois

        steps.append({"type": "step", "node": node_desc})

        if not targets:
            break

        # Caminho linear comum: um único destino exec de saída (ex: "then")
        current_node = targets[0][1]

    return steps


# =============================================================================
# Extração de variáveis e classe pai
# =============================================================================

def _extract_parent_class(blueprint: unreal.Blueprint) -> str:
    parent = _safe_get_editor_property(blueprint, "ParentClass", default=None)
    if parent is None:
        return "Unknown"
    try:
        return parent.get_name()
    except Exception:
        return str(parent)


def _extract_variables(blueprint: unreal.Blueprint) -> list:
    """
    Extrai variáveis do Blueprint via SimpleConstructionScript/generated
    class properties. Usa BlueprintEditorLibrary quando disponível (API
    mais estável através de versões que acessar diretamente a
    UberGraphFrame ou propriedades internas do gerado).
    """
    variables = []
    try:
        var_names = unreal.BlueprintEditorLibrary.get_blueprint_variables(blueprint)
    except Exception:
        # API alternativa não disponível nesta versão/build — degrada
        # graciosamente para lista vazia em vez de derrubar a exportação
        # inteira do Blueprint.
        unreal.log_warning(
            f"Não foi possível extrair variáveis via BlueprintEditorLibrary "
            f"para '{blueprint.get_name()}' — verifique a versão da API."
        )
        return variables

    for var in var_names:
        var_name = str(_safe_get_editor_property(var, "VarName", default=""))
        if not var_name:
            continue
        default_value = _safe_get_editor_property(var, "DefaultValue", default=None)
        variables.append({
            "name": var_name,
            "default_value": str(default_value) if default_value is not None else None,
        })

    return variables


# =============================================================================
# Orquestração principal por Blueprint
# =============================================================================

def _find_entry_nodes(event_graph: unreal.EdGraph) -> list:
    """
    Localiza nós de entrada válidos no grafo: K2Node_Event cujo nome de
    evento/função bate em KNOWN_ENTRY_EVENT_NAMES, OU qualquer
    K2Node_CustomEvent (Custom Events são sempre pontos de entrada válidos,
    independente do nome escolhido pelo usuário).
    """
    entry_nodes = []
    nodes = _safe_get_editor_property(event_graph, "Nodes", default=[])

    for node in nodes:
        class_name = node.get_class().get_name()

        if class_name == "K2Node_CustomEvent":
            entry_nodes.append(node)
            continue

        if class_name == "K2Node_Event":
            event_ref = _safe_get_editor_property(node, "EventReference", default=None)
            if event_ref is not None:
                member_name = str(_safe_get_editor_property(event_ref, "MemberName", default=""))
                if member_name in KNOWN_ENTRY_EVENT_NAMES:
                    entry_nodes.append(node)

    return entry_nodes


def export_blueprint_to_json(asset_path: str) -> bool:
    """
    Carrega um Blueprint por caminho de asset, extrai sua estrutura, e
    salva <NomeDoAsset>.bp.json em OUTPUT_DIR.

    Returns:
        True se exportado com sucesso, False se pulado/falhou (logado).
    """
    blueprint = unreal.EditorAssetLibrary.load_asset(asset_path)
    if blueprint is None or not isinstance(blueprint, unreal.Blueprint):
        unreal.log_warning(f"Asset '{asset_path}' não é um Blueprint válido — pulando.")
        return False

    asset_name = blueprint.get_name()

    result = {
        "AssetName": asset_name,
        "AssetPath": asset_path,
        "ParentClass": _extract_parent_class(blueprint),
        "Variables": _extract_variables(blueprint),
        "ExecutionFlow": [],
    }

    event_graphs = _safe_get_editor_property(blueprint, "UbergraphPages", default=[])
    if not event_graphs:
        unreal.log_warning(f"Blueprint '{asset_name}' não tem EventGraph acessível — exportando sem ExecutionFlow.")
    else:
        visited_global = set()
        for event_graph in event_graphs:
            entry_nodes = _find_entry_nodes(event_graph)
            for entry_node in entry_nodes:
                entry_desc = _describe_node(entry_node)
                flow_steps = _trace_execution_flow(entry_node, visited_global)
                result["ExecutionFlow"].append({
                    "entry_point": entry_desc,
                    "steps": flow_steps,
                })

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, f"{asset_name}.bp.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    unreal.log(f"Exportado: {output_path}")
    return True


def run_export(source_path: str = SOURCE_ASSET_PATH):
    """
    Ponto de entrada. Varre recursivamente `source_path` (caminho de
    Content Browser) em busca de Blueprints e exporta cada um.
    """
    asset_registry = unreal.AssetRegistryHelpers.get_asset_registry()
    asset_datas = asset_registry.get_assets_by_path(source_path, recursive=True)

    blueprint_assets = [
        ad for ad in asset_datas
        if str(ad.asset_class_path.asset_name) in ("Blueprint", "BlueprintGeneratedClass")
    ]

    unreal.log(f"Encontrados {len(blueprint_assets)} Blueprint(s) em '{source_path}'.")

    exported_count = 0
    failed_count = 0
    for asset_data in blueprint_assets:
        asset_path = str(asset_data.package_name)
        try:
            if export_blueprint_to_json(asset_path):
                exported_count += 1
            else:
                failed_count += 1
        except Exception as exc:
            unreal.log_error(f"Falha ao exportar '{asset_path}': {exc}")
            failed_count += 1

    unreal.log(f"Exportação concluída: {exported_count} sucesso(s), {failed_count} falha(s)/pulado(s).")


if __name__ == "__main__":
    run_export()