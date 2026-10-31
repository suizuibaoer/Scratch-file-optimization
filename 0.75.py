import json
import zipfile
import os
import sys
import shutil
import tempfile
import re
import base64
import threading
import urllib.request
import urllib.parse
from collections import defaultdict
from copy import deepcopy


def setup_console_encoding():
    """让中文输出在任何控制台都不崩（Windows / macOS / Linux 通用）。

    策略：
      1. 保留控制台原本的编码（中文 Windows 上是 cp936，直接输出就是对的），
         只把错误策略改成 replace —— 万一某个字符编不出来，用 ? 顶替，
         而不是抛 UnicodeEncodeError 直接把程序干掉。
      2. 如果当前编码连"中文"都编不出来（比如英文 Windows 的 cp437），
         就把 Windows 控制台切到 UTF-8 再输出，尽量让它可读。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    try:
        "中文".encode(getattr(sys.stdout, "encoding", None) or "ascii")
        return                     # 原生编码就能输出中文，保持不动
    except Exception:
        pass

    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ============================================================
#  积木类型判断工具
# ------------------------------------------------------------
#  说明：SB3 的 project.json 里没有"形状/类型"字段，
#        所以"哪块是帽子积木(hat block)"只能靠 opcode 白名单来判定。
#  本工具完全依赖内置列表即可工作（不依赖任何外部文件）：
#        - 内置帽子块 28 个（官方核心 + 官方扩展）
#        - 内置"已知模块前缀"，用于扩展诊断（识别第三方 / 新扩展）
#  可选：若同目录存在 hat_opcodes.json，会自动并入以增强判定精度。
# ============================================================

# 官方核心帽子块：事件类 + 控制类 + 自定义积木定义(My Blocks)
CORE_HAT_OPCODES = {
    "event_whenflagclicked",          # 当绿旗被点击
    "event_whenkeypressed",           # 当按下 [ ] 键
    "event_whenthisspriteclicked",    # 当角色被点击
    "event_whenstageclicked",         # 当舞台被点击
    "event_whenbackdropswitchesto",   # 当背景换成 [ ]
    "event_whengreaterthan",          # 当 [响度] > ()
    "event_whenbroadcastreceived",    # 当接收到 [ ]
    "event_whentouchingobject",       # 旧版/隐藏块（scratch-vm 仍收录，稳妥纳入）
    "control_start_as_clone",         # 当作为克隆体启动时
    "procedures_definition",          # 定义 [ ]（自定义积木入口）
}

# 官方扩展附带的帽子块
EXTENSION_HAT_OPCODES = {
    # 视频侦测
    "videoSensing_whenMotionGreaterThan",
    # Makey Makey
    "makeymakey_whenMakeyKeyPressed",
    "makeymakey_whenCodePressed",
    # micro:bit
    "microbit_whenButtonPressed",
    "microbit_whenGesture",
    "microbit_whenTilted",
    "microbit_whenPinConnected",
    # LEGO EV3
    "ev3_whenButtonPressed",
    "ev3_whenDistanceLessThan",
    "ev3_whenBrightnessLessThan",
    # LEGO BOOST
    "boost_whenColor",
    "boost_whenTilted",
    # LEGO WeDo 2.0
    "wedo2_whenDistance",
    "wedo2_whenTilted",
    # Go Direct Force & Acceleration
    "gdxfor_whenGesture",
    "gdxfor_whenForcePushedOrPulled",
    "gdxfor_whenTilted",
    # 人脸侦测(Face Sensing) 的 when 系列帽子：如项目用到，可在此补充具体 opcode
    # 语音转文字(Speech to Text，未注册进 scratch-vm 内置表)
    "speech2text_whenIHearHat",
}

# 人脸侦测(Face Sensing) 扩展：官方新扩展（scratch-vm 5.0.300 里还没有收录）
FACESENSING_HAT_OPCODES = {
    "faceSensing_whenTilted",
    "faceSensing_whenSpriteTouchesPart",
    "faceSensing_whenFaceDetected",
}
FACESENSING_OPCODES = FACESENSING_HAT_OPCODES | {
    "faceSensing_goToPart",
    "faceSensing_pointInFaceTiltDirection",
    "faceSensing_setSizeToFaceSize",
    "faceSensing_faceIsDetected",
    "faceSensing_faceTilt",
    "faceSensing_faceSize",
}

HAT_OPCODES = CORE_HAT_OPCODES | EXTENSION_HAT_OPCODES | FACESENSING_HAT_OPCODES

# 兜底用：这些前缀确定不含帽子块
SAFE_NON_HAT_PREFIXES = {
    "motion", "looks", "sound", "sensing", "operator", "data", "argument",
    "music", "pen", "text2speech", "translate", "speech2text",
}

# 官方"非 primitive"积木 opcode：不在 scratch-vm 的 _primitives 里，
# 依据 Scratch Wiki《List of Block Opcodes》补录（均为非帽子块）。
BUILTIN_EXTRA_OPCODES = {
    # — 特殊内部输入块（无扩展前缀）—
    "note",                          # 音乐 / EV3 / WeDo 的"音符"输入块
    "matrix",                        # micro:bit 的"点阵"输入块
    "procedures_prototype",
    "argument_editor_boolean",
    "argument_editor_string_number",
    # — 内部菜单 / 下拉块 —
    "motion_goto_menu", "motion_glideto_menu", "motion_pointtowards_menu",
    "looks_costume", "looks_backdrops",
    "sound_sounds_menu",
    "event_broadcast_menu", "event_touchingobjectmenu",
    "control_create_clone_of_menu",
    "sensing_touchingobjectmenu", "sensing_distancetomenu", "sensing_keyoptions",
    "sensing_of_object_menu",
    "music_menu_DRUM", "music_menu_INSTRUMENT",
    "pen_menu_colorParam",
    "videoSensing_menu_ATTRIBUTE", "videoSensing_menu_SUBJECT", "videoSensing_menu_VIDEO_STATE",
    "text2speech_menu_voices", "text2speech_menu_languages",
    "translate_menu_languages",
    "makeymakey_menu_KEY", "makeymakey_menu_SEQUENCE",
    "microbit_menu_buttons", "microbit_menu_gestures",
    "microbit_menu_tiltDirectionAny", "microbit_menu_tiltDirection",
    "microbit_menu_touchPins", "microbit_menu_pinState",
    "gdxfor_menu_gestureOptions", "gdxfor_menu_pushPullOptions",
    "gdxfor_menu_tiltAnyOptions", "gdxfor_menu_tiltOptions", "gdxfor_menu_axisOptions",
    "ev3_menu_motorPorts", "ev3_menu_sensorPorts",
    "boost_menu_MOTOR_ID", "boost_menu_MOTOR_DIRECTION", "boost_menu_MOTOR_REPORTER_ID",
    "boost_menu_COLOR", "boost_menu_TILT_DIRECTION_ANY", "boost_menu_TILT_DIRECTION",
    "wedo2_menu_MOTOR_ID", "wedo2_menu_MOTOR_DIRECTION", "wedo2_menu_OP",
    "wedo2_menu_TILT_DIRECTION_ANY", "wedo2_menu_TILT_DIRECTION",
    # — 隐藏块补充 —
    "data_listindexall", "data_listindexrandom",
    "procedures_declaration", "sensing_online",
    # — TurboWarp 积木（内置扩展 tw）—
    "tw_getLastKeyPressed",          # 最后按下的按键
    "tw_getButtonIsDown",            # 按下 xx 键?
}

# 额外"已知"扩展前缀：自身能识别，但没有完整积木清单
#   tw —— TurboWarp 的"TurboWarp 积木"扩展（内置，无 URL 可下载）
EXTRA_KNOWN_EXTENSION_PREFIXES = {"tw"}

# 内置"已知模块前缀" = 帽子块前缀 ∪ 安全非帽子前缀 ∪ 无前缀内部块 ∪ 额外已知扩展
# 用途：扩展诊断 —— 积木里出现、但不在此集合中的前缀 = 未覆盖的第三方/新扩展
BUILTIN_KNOWN_PREFIXES = (SAFE_NON_HAT_PREFIXES | {"note", "matrix"}
                          | EXTRA_KNOWN_EXTENSION_PREFIXES | {
    "event", "control", "procedures",
    "videoSensing", "makeymakey", "microbit", "ev3", "boost", "wedo2", "gdxfor",
    "faceSensing",
})

# 全量"已知积木"opcode 表（内置数据见文件末尾的"内置积木数据"段；此处先占位）
ALL_OPCODES = set()
# 已知模块前缀集合（内置基线 + hat_opcodes.json 补充）
KNOWN_PREFIXES = set(BUILTIN_KNOWN_PREFIXES)


def _opcode_prefix(opcode):
    """取 opcode 的模块前缀，例如 motion_movesteps -> motion。"""
    if isinstance(opcode, str):
        return opcode.split("_", 1)[0]
    return ""


def _load_block_meta():
    """【可选】若同目录存在 hat_opcodes.json，则并入以增强判定精度。

    该文件不是必需的：内置数据（见文件末尾"内置积木数据"段）已经足够。
    存在时：把其中的帽子表并入 HAT_OPCODES，把 opcode / 前缀分别并入
    ALL_OPCODES / KNOWN_PREFIXES。
    返回合并后的帽子总数；未找到文件时返回 None。
    """
    global HAT_OPCODES, ALL_OPCODES, KNOWN_PREFIXES
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hat_opcodes.json")
    if not os.path.exists(json_path):
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    hats = data.get("hatOpcodes")
    alls = data.get("allOpcodes")
    if isinstance(hats, list):
        HAT_OPCODES = set(HAT_OPCODES) | set(hats)
    if isinstance(alls, list):
        ALL_OPCODES = set(ALL_OPCODES) | set(alls)
        KNOWN_PREFIXES = set(KNOWN_PREFIXES) | {_opcode_prefix(op) for op in alls}
    return len(HAT_OPCODES)


def is_hat_opcode(opcode) -> bool:
    """只给一个 opcode 字符串时判断它是否是帽子积木。"""
    return isinstance(opcode, str) and opcode in HAT_OPCODES


def is_hat_block(block) -> bool:
    """判断 project.json 中的一块积木(dict)是否是帽子积木。

    注意：帽子积木一定是顶块(parent 为空)，但顶块不一定是帽子块
          （单独摆放的 reporter 也是顶块），所以这里按 opcode 判定。
    """
    return isinstance(block, dict) and is_hat_opcode(block.get("opcode"))


def get_target_label(target, target_idx):
    if not isinstance(target, dict):
        return f"目标#{target_idx}"
    name = target.get('name') or f"目标#{target_idx}"
    if target.get('isStage'):
        return f"舞台({name})"
    return f"角色({name})"


def build_call_graph(project_data):
    """ 构建【逐目标独立】的自定义积木调用关系图

    返回:
        definitions : {(target_idx, proccode): (def_block_id, proto_block_id)}
        call_edges  : {(target_idx, caller_proccode): {(target_idx, callee_proccode), ...}}
        call_counts : {(target_idx, proccode): int}
        entry_points: {(target_idx, proccode), ...}  被事件/控制流直接调用的函数（入口）
    """
    definitions = {}
    call_edges = defaultdict(set)
    call_counts = defaultdict(int)
    entry_points = set()

    targets = project_data.get('targets', [])
    for target_idx, target in enumerate(targets):
        if not isinstance(target, dict):
            continue
        blocks = target.get('blocks', {})
        if not isinstance(blocks, dict):
            # 舞台 / 角色没有积木时直接跳过
            continue

        # —— 第一遍：收集该目标内的所有函数定义 ——
        for block_id, block in blocks.items():
            if not isinstance(block, dict):
                continue
            if block.get('opcode') == 'procedures_definition':
                proto_ref = block.get('inputs', {}).get('custom_block', [None])
                proto_id = proto_ref[1] if isinstance(proto_ref, list) and len(proto_ref) > 1 else None
                if proto_id and isinstance(blocks.get(proto_id), dict):
                    proccode = blocks[proto_id].get('mutation', {}).get('proccode', '')
                    if proccode:
                        # 关键：以 (目标索引, 积木名) 为键，舞台与角色不会互相覆盖
                        definitions[(target_idx, proccode)] = (block_id, proto_id)

        # —— 第二遍：收集该目标内的所有调用，并判断调用者上下文 ——
        for block_id, block in blocks.items():
            if not isinstance(block, dict):
                continue
            if block.get('opcode') != 'procedures_call':
                continue
            callee = block.get('mutation', {}).get('proccode', '')
            if not callee:
                continue
            call_counts[(target_idx, callee)] += 1

            # 向上追溯父块，确定这个 call 是在哪个函数体内，还是在顶层
            parent_id = block.get('parent')
            caller_func = None
            visited_parents = set()
            cur = parent_id
            while cur and isinstance(cur, str) and cur not in visited_parents:
                visited_parents.add(cur)
                parent_block = blocks.get(cur, {})
                if not isinstance(parent_block, dict):
                    break
                if parent_block.get('opcode') == 'procedures_definition':
                    p_proto_ref = parent_block.get('inputs', {}).get('custom_block', [None])
                    p_proto_id = p_proto_ref[1] if isinstance(p_proto_ref, list) and len(p_proto_ref) > 1 else None
                    if p_proto_id and isinstance(blocks.get(p_proto_id), dict):
                        caller_func = blocks[p_proto_id].get('mutation', {}).get('proccode', '')
                        break
                cur = parent_block.get('parent')

            if caller_func:
                # 调用发生在本目标的某个函数体内 -> 记一条本目标内部的边
                call_edges[(target_idx, caller_func)].add((target_idx, callee))
            else:
                # 被事件/控制流等非函数块直接调用 -> 入口函数
                entry_points.add((target_idx, callee))

    return definitions, call_edges, call_counts, entry_points


def find_reachable(entry_points, call_edges):
    """ 从入口点出发，BFS 找出所有可达的函数（节点为 (target_idx, proccode) 元组） """
    reachable = set()
    queue = list(entry_points)
    while queue:
        node = queue.pop(0)  # 使用 pop(0) 实现 FIFO 队列
        if node in reachable:
            continue
        reachable.add(node)
        for callee in call_edges.get(node, set()):
            if callee not in reachable:
                queue.append(callee)
    return reachable


def remove_procedure_blocks(blocks, def_block_id, proto_block_id):
    """ 安全删除一个函数的所有相关块，返回删除数量 """
    to_delete = set()
    to_delete.add(proto_block_id)
    to_delete.add(def_block_id)

    # 收集 definition 的参数标签块
    def_block = blocks.get(def_block_id, {})
    if isinstance(def_block, dict):
        for inp_val in def_block.get('inputs', {}).values():
            if isinstance(inp_val, list) and len(inp_val) > 1 and isinstance(inp_val[1], str):
                to_delete.add(inp_val[1])

    # 沿 next 链和 inputs 子树递归收集
    stack = list(to_delete)
    visited = set()
    while stack:
        bid = stack.pop()
        if bid in visited or not isinstance(bid, str):
            continue
        visited.add(bid)
        b = blocks.get(bid, {})
        if not isinstance(b, dict):
            continue
        nxt = b.get('next')
        if isinstance(nxt, str) and nxt in blocks:
            to_delete.add(nxt)
            stack.append(nxt)
        for iv in b.get('inputs', {}).values():
            if isinstance(iv, list) and len(iv) > 1 and isinstance(iv[1], str):
                child = iv[1]
                if child in blocks and child not in to_delete:
                    to_delete.add(child)
                    stack.append(child)

    for bid in to_delete:
        blocks.pop(bid, None)
    return len(to_delete)


def clean_unused_procedures(project_data):
    """ 主清理函数：移除未使用的自定义积木（舞台 + 角色逐目标独立处理） """
    definitions, call_edges, call_counts, entry_points = build_call_graph(project_data)
    if not definitions:
        return project_data, 0

    reachable = find_reachable(entry_points, call_edges)
    unused = set(definitions.keys()) - reachable
    if not unused:
        return project_data, 0

    new_targets = deepcopy(project_data.get('targets', []))
    total_removed_funcs = 0

    # 按 (目标索引, 积木名) 顺序处理，舞台(idx 小)会排在前面
    for key in sorted(unused, key=lambda k: (k[0], k[1])):
        target_idx, proccode = key
        def_id, proto_id = definitions[key]
        blocks = new_targets[target_idx]['blocks']
        n = remove_procedure_blocks(blocks, def_id, proto_id)
        total_removed_funcs += 1
        label = get_target_label(new_targets[target_idx], target_idx)
        print(f" 移除: {label} 中的 [{proccode}] ({n}个块)")

    project_data['targets'] = new_targets
    return project_data, total_removed_funcs


def get_compression_level():
    print("\n请选择压缩等级 (0-9):")
    print(" 0 : 仅存储(速度最快)")
    print(" 1 : 最快压缩")
    print(" 6 : 平衡(推荐)")
    print(" 9 : 最大压缩(速度最慢)")
    while True:
        choice = input("请输入数字 [默认为9]: ").strip()
        if choice == "":
            return 9
        try:
            level = int(choice)
            if 0 <= level <= 9:
                return level
            print(" 请输入 0~9 之间的整数")
        except ValueError:
            print(" 请输入数字")


def ask_cleaned_suffix():
    print("\n是否为输出文件名添加 \"_cleaned\" ?")
    while True:
        choice = input(" [y/N]: ").strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("", "n", "no"):
            return False
        print(" 请输入 Y 或 N")


def ask_operations():
    """询问用户要执行哪些操作。

    返回 dict：{"shadow": bool, "funcs": bool, "dead": bool}
    （分别为：清理阴影值 / 移除未使用函数 / 清理死块与孤儿块）
    """

    def ask(question, default_yes=True):
        print(question)
        hint = " [Y/n]: " if default_yes else " [y/N]: "
        while True:
            choice = input(hint).strip().lower()
            if choice in ("y", "yes"):
                return True
            if choice in ("", "n", "no"):
                return default_yes if choice == "" else False
            print(" 请输入 Y 或 N")

    operations = {}
    operations["shadow"] = ask("是否要清理阴影值(被别的积木覆盖的值)?")
    operations["funcs"] = ask("\n是否要移除未使用的函数?")
    operations["dead"] = ask("\n是否要清理孤儿/死积木(顶层非帽子块、parent 断链块及其子树)?")
    return operations


def ask_extension_network():
    """询问是否分析“需要联网下载”的第三方扩展源码。

    返回 True（允许联网）或 False（只分析内嵌 data: 扩展）。
    """
    print("\n是否分析第三方扩展源码(对需要联网下载的扩展)?")
    print(" 说明: 会按 project.json 的 extensionURLs 下载扩展 JS 并静态解析；")
    print("       内嵌(data:)的扩展始终会分析，无需联网。")
    while True:
        choice = input(" [Y/n]: ").strip().lower()
        if choice in ("", "y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        print(" 请输入 Y 或 N")


def clean_all_shadow_values(project_data):
    # 4=数字 5=正数 6=整数 7=整数 8=角度 9=颜色 10=文本
    # 11=广播 12=变量 13=列表
    # 说明：阴影值清理本身就是遍历 project['targets'] 下所有目标，
    #       舞台与角色都会被处理。
    NUMERIC_PRIMITIVES = (4, 5, 6, 7, 8, 9, 10)
    cleaned_count = 0

    for target in project_data.get("targets", []):
        if not isinstance(target, dict):
            continue
        for block in target.get("blocks", {}).values():
            if not isinstance(block, dict):
                continue
            for inp in block.get("inputs", {}).values():
                if not isinstance(inp, list) or len(inp) < 2:
                    continue
                if inp[0] != 3:
                    continue
                if len(inp) < 3 or not (isinstance(inp[2], list) and len(inp[2]) == 2):
                    continue
                prim_type, prim_value = inp[2]
                if prim_type in NUMERIC_PRIMITIVES and prim_value != "":
                    inp[2][1] = ""
                    cleaned_count += 1
    return project_data, cleaned_count


# ============================================================
#  死块 / 孤儿块清理
# ------------------------------------------------------------
#  思路：从"活根"出发做可达性遍历。
#    活根 = 帽子积木；以及"无法确定为非帽子"的积木
#           （保守起见视为活块，避免误删改版/扩展里未知的帽子块）
#    判定优先级：HAT_OPCODES(帽子) > ALL_OPCODES(已知非帽子=死块) > 未知则保护
#    从所有活根沿 next + inputs 收集可达块，其余不可达的块即为
#    "顶层的非帽子死脚本" + "parent 断链的孤儿块"（连同整棵子树）。
#  安全性：被删块一定无法从任何活根到达，因此保留下来的块
#          不会引用到被删块，不会引入新的悬空引用。
# ============================================================

def _is_known_non_hat(opcode):
    """判断某 opcode 是否"确定不是帽子块"（确定不是 → 才敢当作死块删除）。

    - 有权威表(ALL_OPCODES)时：不在帽子表、却在已知积木表里 → 确定非帽子
    - 无权威表时：退化为前缀白名单（SAFE_NON_HAT_PREFIXES）
    """
    if not isinstance(opcode, str):
        return False
    if opcode in HAT_OPCODES:
        return False
    if ALL_OPCODES:
        return opcode in ALL_OPCODES
    return _opcode_prefix(opcode) in SAFE_NON_HAT_PREFIXES


def _collect_reachable(blocks, start_ids):
    """从 start_ids 出发，沿 next + inputs 收集所有可达的块 id。

    注意：挤压(数组)形式的块（如 [12,"a",id,x,y] 这种变量 reporter）也要算作可达，
    否则会把“被脚本引用的变量/列表块”当成死块误删。
    """
    reachable = set()
    stack = list(start_ids)
    while stack:
        bid = stack.pop()
        if not isinstance(bid, str) or bid in reachable:
            continue
        block = blocks.get(bid)
        if block is None:
            continue
        reachable.add(bid)
        if not isinstance(block, dict):
            continue          # 数组形式没有 next / inputs，标记为可达即可
        nxt = block.get("next")
        if isinstance(nxt, str):
            stack.append(nxt)
        for inp in block.get("inputs", {}).values():
            if isinstance(inp, list):
                for item in inp:
                    if isinstance(item, str) and item in blocks:
                        stack.append(item)
    return reachable


def find_dead_blocks(blocks):
    """返回该目标内所有"死块/孤儿块"的 id 集合（只读，不修改 blocks）。"""
    if not isinstance(blocks, dict):
        return set()

    # 根块：没有父块，或父块已不存在(断链)
    roots = []
    for bid, block in blocks.items():
        if not isinstance(block, dict):
            continue
        parent = block.get("parent")
        if parent is None or parent not in blocks:
            roots.append(bid)

    # 挑出"活根"：帽子积木；以及无法确定为非帽子块的（保守保护）
    live_roots = []
    for bid in roots:
        block = blocks[bid]
        if not _is_known_non_hat(block.get("opcode")):
            live_roots.append(bid)

    reachable = _collect_reachable(blocks, live_roots)
    return {bid for bid in blocks if bid not in reachable}


def clean_dead_blocks(project_data):
    """移除死块/孤儿块（顶层非帽子块、parent 断链块及其子树），返回删除数量。"""
    removed_count = 0

    for target in project_data.get("targets", []):
        if not isinstance(target, dict):
            continue
        blocks = target.get("blocks", {})
        if not isinstance(blocks, dict) or not blocks:
            continue

        dead_ids = find_dead_blocks(blocks)
        if not dead_ids:
            continue

        for bid in dead_ids:
            blocks.pop(bid, None)
        removed_count += len(dead_ids)

        # 顺带移除挂在被删积木上的注释，避免留下悬空 comment
        comments = target.get("comments")
        if isinstance(comments, dict):
            for cid in [c for c, cm in comments.items()
                        if isinstance(cm, dict) and cm.get("blockId") in dead_ids]:
                comments.pop(cid, None)

    return project_data, removed_count


def diagnose_extensions(project_data):
    """扩展诊断：返回"当前积木表未覆盖"的扩展前缀列表。

    - 依据：项目的 extensions 字段 + 积木里实际出现的 opcode 前缀
    - 判定：前缀不在 KNOWN_PREFIXES 里 → 未覆盖
    - 含义：未覆盖扩展的积木会走"未知 → 保守保留"，其顶层死块不会被清理
    """
    if not KNOWN_PREFIXES:
        return []

    candidates = set()
    declared = project_data.get("extensions")
    if isinstance(declared, list):
        candidates.update(x for x in declared if isinstance(x, str))

    for target in project_data.get("targets", []):
        if not isinstance(target, dict):
            continue
        blocks = target.get("blocks")
        if not isinstance(blocks, dict):
            continue
        for block in blocks.values():
            if isinstance(block, dict):
                candidates.add(_opcode_prefix(block.get("opcode")))

    return sorted(p for p in candidates if p and p not in KNOWN_PREFIXES)


# ============================================================
#  第三方扩展源码分析（获取扩展代码 → 静态解析其中的积木类型）
# ------------------------------------------------------------
#  数据来源：TurboWarp 等改版会把第三方扩展记录在 project.json 的
#           extensionURLs（扩展id -> URL）；也支持内嵌的 data: URL。
#  解析方式：只做“静态文本分析”，绝不执行未知扩展代码。
#  安全性：解析结果只做“加法”并入 HAT_OPCODES / ALL_OPCODES；
#           失败或解析为空 → 不并入，继续按“未知 → 保守保护”处理。
# ============================================================

EXT_ANALYZE_ENABLED = True                    # 总开关：是否分析扩展源码
EXT_ALLOW_NETWORK = True                      # 是否允许联网下载（由启动时询问设定）
EXT_FETCH_TIMEOUT = 15                        # 单个 URL 下载超时（秒）
EXT_MAX_BYTES = 5 * 1024 * 1024               # 单个扩展源码大小上限（5MB）
EXT_POLL_INTERVAL = 0.1                       # 等待下载时的轮询间隔（秒）
EXT_UA = "Mozilla/5.0 (compatible; sb3-optimizer/1.0)"
EXT_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              ".ext_cache", "extensions.json")

# 用户按回车主动跳过某扩展时的返回值标记
EXT_SKIPPED = object()

# 视为“脚本起点”的块类型：TurboWarp 里 hat 与 event 都可用于启动脚本
SCRIPT_STARTER_TYPES = {"hat", "event"}

_ext_cache = None


def _load_ext_cache():
    """懒加载扩展解析缓存（URL -> {opcodes, hats}）。"""
    global _ext_cache
    if _ext_cache is None:
        try:
            with open(EXT_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _ext_cache = data if isinstance(data, dict) else {}
        except Exception:
            _ext_cache = {}
    return _ext_cache


def _save_ext_cache():
    if _ext_cache is None:
        return
    try:
        os.makedirs(os.path.dirname(EXT_CACHE_PATH), exist_ok=True)
        with open(EXT_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_ext_cache, f, ensure_ascii=False)
    except Exception:
        pass


def _decode_data_url(url):
    """解码 data: URL 里的扩展源码，失败返回 None。"""
    try:
        header, _, payload = url[len("data:"):].partition(",")
        if ";base64" in header.lower():
            return base64.b64decode(payload).decode("utf-8", "replace")
        return urllib.parse.unquote(payload)
    except Exception:
        return None


def _fetch_url(url):
    """下载扩展源码文本（只取文本，不执行）。

    返回 (源码文本 或 None, 错误信息 或 None)。
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": EXT_UA})
        with urllib.request.urlopen(req, timeout=EXT_FETCH_TIMEOUT) as resp:
            raw = resp.read(EXT_MAX_BYTES + 1)
        if len(raw) > EXT_MAX_BYTES:
            return None, "源码过大"
        return raw.decode("utf-8", "replace"), None
    except Exception as e:
        return None, str(e)


def _enter_pressed():
    """非阻塞检测：当前是否有回车在等待输入（Windows / macOS / Linux 都能用）。

    - Windows：msvcrt.kbhit / getwch 直接读控制台缓冲区
    - macOS / Linux：select + termios 把 stdin 临时切到非规范模式读一个字符
    两头都不支持时返回 False —— 只是"回车跳过"这一个小特性失效，不影响其它功能。
    """
    # —— Windows ——
    try:
        import msvcrt
        pressed = False
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                pressed = True
        return pressed
    except ImportError:
        pass                       # 不是 Windows，往下走 POSIX 分支
    except Exception:
        return False

    # —— macOS / Linux ——
    try:
        import select
        import termios
        import tty
        if not sys.stdin.isatty():
            return False           # stdin 被重定向了，没有"按键"可言
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)      # 关掉行缓冲与回显（Ctrl-C 仍然有效）
            pressed = False
            while select.select([sys.stdin], [], [], 0)[0]:
                ch = sys.stdin.read(1)
                if ch in ("\n", "\r"):
                    pressed = True
                elif ch == "":
                    break
            return pressed
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)   # 一定要还原
    except Exception:
        return False


def _fetch_url_interactive(url):
    """带"按回车跳过"的联网下载。

    返回 (源码 或 None, 错误 或 None, 是否被跳过)。
    """
    box = {}

    def worker():
        box["text"], box["err"] = _fetch_url(url)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    while t.is_alive():
        t.join(EXT_POLL_INTERVAL)
        if _enter_pressed():
            return None, None, True
    return box.get("text"), box.get("err"), False


def _decode_embedded_sources(js, depth=0):
    """提取 JS 文本里"内嵌打包"的 base64 源码（如 ccw2tw 之类打包器）。

    很多改版扩展的源码是以 base64 字符串内嵌、运行时才 atob 出来的，
    直接扫原文会什么都找不到。这里把所有"长得像 base64 且解出来是 JS
    （含 opcode/blockType/getInfo）"的片段解出来，并递归处理。
    """
    if depth >= 3:
        return []
    found = []
    for m in re.finditer(r"['\"]([A-Za-z0-9+/=\s]{150,})['\"]", js):
        blob = re.sub(r"\s+", "", m.group(1))
        if len(blob) < 150:
            continue
        pad = (-len(blob)) % 4
        try:
            raw = base64.b64decode(blob + "=" * pad, validate=True)
        except Exception:
            continue
        try:
            text = raw.decode("utf-8")
        except Exception:
            continue
        if "opcode" in text or "blockType" in text or "getInfo" in text:
            found.append(text)
            found.extend(_decode_embedded_sources(text, depth + 1))
    return found


def _extract_block_types(js):
    """静态扫描扩展 JS，返回 [(opcode, blocktype_or_None), ...]。

    思路：定位每个 opcode，再在其所在“块对象”范围内找 blockType。
    支持 `blockType: 'hat'`、`blockType: Scratch.BlockType.HAT`、`BlockType.HAT`
    （取最后一个点段再转小写）；找不到则视作未声明（默认 command）。
    """
    results = []
    for m in re.finditer(r"opcode\s*:\s*['\"]([A-Za-z0-9_]+)['\"]", js):
        opcode = m.group(1)
        i = m.end()
        depth = 0
        block_type = None
        n = len(js)
        while i < n and (i - m.end()) < 8000:
            ch = js[i]
            if ch == "{":
                depth += 1
                i += 1
            elif ch == "}":
                if depth == 0:
                    break
                depth -= 1
                i += 1
            elif ch == '"' or ch == "'" or ch == "`":
                quote = ch
                i += 1
                while i < n and js[i] != quote:
                    if js[i] == "\\":
                        i += 1
                    i += 1
                i += 1
            elif depth == 0 and js.startswith("blockType", i):
                bm = re.match(
                    r"blockType\s*:\s*(?:\"([^\"]*)\"|'([^']*)'|([A-Za-z0-9_$.]+))",
                    js[i:i + 160])
                if bm:
                    token = bm.group(1) or bm.group(2) or bm.group(3) or ""
                    block_type = token.split(".")[-1].strip("\"'").lower()
                    break
                i += 1
            else:
                i += 1
        results.append((opcode, block_type))
    return results


def analyze_extension_source(url, ext_id=None, interactive=True):
    """解析指定 URL 的扩展源码，返回 (块opcode集合, 帽子opcode集合)（不含扩展前缀）。

    - 结果按 URL 缓存（命中缓存时静默返回）
    - 联网下载时（interactive=True）可按回车跳过 → 返回 EXT_SKIPPED
    - 下载 / 解码 / 解析失败 → 返回 None
    """
    cache = _load_ext_cache()
    entry = cache.get(url)
    if isinstance(entry, dict):
        return set(entry.get("opcodes") or []), set(entry.get("hats") or [])

    label = f"[{ext_id}]" if ext_id else f"[{url}]"
    if url.startswith("data:"):
        print(f" 分析扩展 {label}（内嵌源码）...", end="", flush=True)
        source = _decode_data_url(url)
        err = None if source else "解码失败"
        skipped = False
    elif interactive:
        print(f" 分析扩展 {label}（联网下载，按回车跳过）...", end="", flush=True)
        source, err, skipped = _fetch_url_interactive(url)
    else:
        source, err = _fetch_url(url)
        skipped = False

    if skipped:
        print(" 已跳过")
        return EXT_SKIPPED
    if not source:
        print(f" 失败（{err}）")
        return None

    # 有些扩展被"打包"过（源码以 base64 内嵌），先把内嵌片段也解出来一起扫
    embedded = _decode_embedded_sources(source)
    opcodes, hats = set(), set()
    for src in [source] + embedded:
        for block_opcode, block_type in _extract_block_types(src):
            opcodes.add(block_opcode)
            if block_type in SCRIPT_STARTER_TYPES:
                hats.add(block_opcode)

    cache[url] = {"opcodes": sorted(opcodes), "hats": sorted(hats)}
    _save_ext_cache()
    extra = f"（含 {len(embedded)} 个内嵌打包片段）" if embedded else ""
    print(f" 完成：{len(opcodes)} 块 / {len(hats)} 帽子{extra}")
    return opcodes, hats


def collect_extension_urls(project_data):
    """收集 project.json 里的 extensionURLs（扩展id -> URL）。"""
    urls = {}
    holders = [project_data]
    holders += [t for t in project_data.get("targets", []) if isinstance(t, dict)]
    for holder in holders:
        ext_urls = holder.get("extensionURLs")
        if isinstance(ext_urls, dict):
            for ext_id, url in ext_urls.items():
                if isinstance(ext_id, str) and isinstance(url, str):
                    urls.setdefault(ext_id, url)
    return urls


def analyze_project_extensions(project_data, interactive=True):
    """下载并静态分析项目用到的第三方扩展，把结果并入全局已知表。

    - data: 内嵌扩展始终分析；需联网下载的仅在 EXT_ALLOW_NETWORK 为真时分析。
    - 返回 (新增帽子数, 新增块数, 失败的扩展id列表)。
    """
    global HAT_OPCODES, ALL_OPCODES, KNOWN_PREFIXES
    if not EXT_ANALYZE_ENABLED:
        return 0, 0, []

    new_hats = new_blocks = 0
    failed = []
    for ext_id, url in sorted(collect_extension_urls(project_data).items()):
        if not url.startswith("data:") and not EXT_ALLOW_NETWORK:
            continue                      # 用户选择：不分析需联网下载的扩展
        result = analyze_extension_source(url, ext_id=ext_id, interactive=interactive)
        if result is EXT_SKIPPED:         # 用户按回车跳过（不算失败）
            continue
        if result is None:                # 下载 / 解析失败
            failed.append(ext_id)
            continue
        block_opcodes, hat_opcodes = result
        if not block_opcodes:
            failed.append(ext_id)
            continue
        for op in block_opcodes:
            full = f"{ext_id}_{op}"
            if full not in ALL_OPCODES:
                ALL_OPCODES.add(full)
                new_blocks += 1
        for op in hat_opcodes:
            full = f"{ext_id}_{op}"
            if full not in HAT_OPCODES:
                HAT_OPCODES.add(full)
                new_hats += 1
        KNOWN_PREFIXES.add(ext_id)
    return new_hats, new_blocks, failed


def process_single_file(sb3_path, output_path, compression_level, operations):
    """ 处理单个文件的完整流程 """
    temp_dir = os.path.join(tempfile.gettempdir(), f"temp_sb3_opt_{os.getpid()}")
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)
    try:
        # 1. 解压
        with zipfile.ZipFile(sb3_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)

        json_path = os.path.join(temp_dir, "project.json")
        if not os.path.exists(json_path):
            print(" 未找到 project.json")
            return False, 0, 0, 0

        # 2. 读取 JSON
        with open(json_path, 'r', encoding='utf-8') as f:
            project = json.load(f)

        # 2.5 第三方扩展：下载并静态分析源码（默认联网，见 EXT_ANALYZE_ENABLED）
        new_hats, new_blocks, ext_failed = analyze_project_extensions(project)
        if new_hats or new_blocks:
            print(f" 扩展分析: 新识别 {new_hats} 个帽子 / {new_blocks} 个积木")
        if ext_failed:
            print(f" 扩展分析失败(保守保留): {', '.join(sorted(ext_failed))}")

        # 2.6 扩展诊断（统计仍未被覆盖的扩展）
        unknown_exts = diagnose_extensions(project)
        if unknown_exts:
            print(f" 未覆盖扩展(其积木按“未知”保守保留): {', '.join(unknown_exts)}")

        removed_funcs = 0
        cleaned_shadows = 0
        removed_dead = 0

        # 3. 根据用户选择执行操作（顺序：函数 → 死块 → 阴影）
        if operations.get("funcs"):
            project, removed_funcs = clean_unused_procedures(project)

        if operations.get("dead"):
            project, removed_dead = clean_dead_blocks(project)

        if operations.get("shadow"):
            project, cleaned_shadows = clean_all_shadow_values(project)

        # 4. 保存 JSON
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(project, f, ensure_ascii=False)

        # 5. 重新打包
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=compression_level) as zipf:
            for root, _, files in os.walk(temp_dir):
                for file in files:
                    fp = os.path.join(root, file)
                    zipf.write(fp, os.path.relpath(fp, temp_dir))

        return True, removed_funcs, cleaned_shadows, removed_dead

    except Exception as e:
        print(f" 处理错误: {e}")
        return False, 0, 0, 0
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def find_sb3_files(path):
    sb3_files = []
    for root, _, files in os.walk(path):
        for f in files:
            if f.lower().endswith('.sb3'):
                sb3_files.append(os.path.join(root, f))
    sb3_files.sort()
    return sb3_files


def build_output_path(input_root, sb3_path, output_root, add_suffix):
    rel_path = os.path.relpath(sb3_path, input_root)
    if add_suffix:
        base_name = os.path.splitext(rel_path)[0]
        output_rel = f"{base_name}_cleaned.sb3"
    else:
        output_rel = rel_path
    return os.path.join(output_root, output_rel)


def describe_operations(operations):
    """把用户勾选的操作拼成一句执行说明。"""
    labels = []
    if operations.get("funcs"):
        labels.append("移除未使用函数")
    if operations.get("dead"):
        labels.append("清理死块")
    if operations.get("shadow"):
        labels.append("清理阴影值")
    return " + ".join(labels) if labels else "无操作"


def process_batch(input_root, sb3_files, output_root, compression_level, add_suffix, operations):
    total = len(sb3_files)
    if total == 0:
        return

    success_count = 0
    fail_count = 0
    total_funcs = 0
    total_shadows = 0
    total_dead = 0

    suffix_label = "_cleaned" if add_suffix else "(原名)"
    operation_label = describe_operations(operations)

    print(f"共发现 {total} 个 .sb3 文件待处理")
    print(f"输入根目录: {input_root}")
    print(f"输出根目录: {output_root}")
    print(f"执行操作: {operation_label}")
    print(f"扩展分析: {'联网下载' if EXT_ALLOW_NETWORK else '仅内嵌(data:)扩展'}")
    print(f"文件名后缀: {suffix_label}")
    print(f"压缩等级: {compression_level}")

    for idx, sb3_path in enumerate(sb3_files, 1):
        output_path = build_output_path(input_root, sb3_path, output_root, add_suffix)
        output_rel = os.path.relpath(output_path, output_root)
        print(f"[{idx}/{total}] {os.path.relpath(sb3_path, input_root)}")
        ok, r_funcs, r_shadows, r_dead = process_single_file(sb3_path, output_path, compression_level, operations)
        if ok:
            success_count += 1
            total_funcs += r_funcs
            total_shadows += r_shadows
            total_dead += r_dead
            result_msg = []
            if operations.get("funcs"):
                result_msg.append(f"移除函数: {r_funcs}")
            if operations.get("dead"):
                result_msg.append(f"清理死块: {r_dead}")
            if operations.get("shadow"):
                result_msg.append(f"清理阴影: {r_shadows}")
            print(f" {', '.join(result_msg)} -> {output_rel}")
        else:
            fail_count += 1
            print(f" 处理失败")

    print(f"处理完成！成功: {success_count}, 失败: {fail_count}")
    if operations.get("funcs"):
        print(f"总计移除未使用函数: {total_funcs}")
    if operations.get("dead"):
        print(f"总计清理死块: {total_dead}")
    if operations.get("shadow"):
        print(f"总计清理阴影值: {total_shadows}")
    print(f"输出目录: {output_root}")


# ======================================================================
#  内置积木数据（由 hat导出/export_hat_opcodes.js 从 scratch-vm 自动生成）
#  说明：让工具无需任何外部文件即可精确判定死块；本段请勿手改。
# ======================================================================
BUILTIN_ALL_OPCODES = """
argument_reporter_boolean
argument_reporter_string_number
boost_getMotorPosition
boost_getTiltAngle
boost_motorOff
boost_motorOn
boost_motorOnFor
boost_motorOnForRotation
boost_seeingColor
boost_setLightHue
boost_setMotorDirection
boost_setMotorPower
boost_whenColor
boost_whenTilted
control_all_at_once
control_clear_counter
control_create_clone_of
control_delete_this_clone
control_for_each
control_forever
control_get_counter
control_if
control_if_else
control_incr_counter
control_repeat
control_repeat_until
control_stop
control_wait
control_wait_until
control_while
data_addtolist
data_changevariableby
data_deletealloflist
data_deleteoflist
data_hidelist
data_hidevariable
data_insertatlist
data_itemnumoflist
data_itemoflist
data_lengthoflist
data_listcontainsitem
data_listcontents
data_replaceitemoflist
data_setvariableto
data_showlist
data_showvariable
data_variable
ev3_beep
ev3_buttonPressed
ev3_getBrightness
ev3_getDistance
ev3_getMotorPosition
ev3_motorSetPower
ev3_motorTurnClockwise
ev3_motorTurnCounterClockwise
ev3_whenBrightnessLessThan
ev3_whenButtonPressed
ev3_whenDistanceLessThan
event_broadcast
event_broadcastandwait
event_whengreaterthan
event_whentouchingobject
gdxfor_getAcceleration
gdxfor_getForce
gdxfor_getSpinSpeed
gdxfor_getTilt
gdxfor_isFreeFalling
gdxfor_isTilted
gdxfor_whenForcePushedOrPulled
gdxfor_whenGesture
gdxfor_whenTilted
looks_backdropnumbername
looks_changeeffectby
looks_changesizeby
looks_changestretchby
looks_cleargraphiceffects
looks_costumenumbername
looks_goforwardbackwardlayers
looks_gotofrontback
looks_hide
looks_hideallsprites
looks_nextbackdrop
looks_nextcostume
looks_say
looks_sayforsecs
looks_seteffectto
looks_setsizeto
looks_setstretchto
looks_show
looks_size
looks_switchbackdropto
looks_switchbackdroptoandwait
looks_switchcostumeto
looks_think
looks_thinkforsecs
makeymakey_whenCodePressed
makeymakey_whenMakeyKeyPressed
microbit_displayClear
microbit_displaySymbol
microbit_displayText
microbit_getTiltAngle
microbit_isButtonPressed
microbit_isTilted
microbit_whenButtonPressed
microbit_whenGesture
microbit_whenPinConnected
microbit_whenTilted
motion_align_scene
motion_changexby
motion_changeyby
motion_direction
motion_glidesecstoxy
motion_glideto
motion_goto
motion_gotoxy
motion_ifonedgebounce
motion_movesteps
motion_pointindirection
motion_pointtowards
motion_scroll_right
motion_scroll_up
motion_setrotationstyle
motion_setx
motion_sety
motion_turnleft
motion_turnright
motion_xposition
motion_xscroll
motion_yposition
motion_yscroll
music_changeTempo
music_getTempo
music_midiPlayDrumForBeats
music_midiSetInstrument
music_playDrumForBeats
music_playNoteForBeats
music_restForBeats
music_setInstrument
music_setTempo
operator_add
operator_and
operator_contains
operator_divide
operator_equals
operator_gt
operator_join
operator_length
operator_letter_of
operator_lt
operator_mathop
operator_mod
operator_multiply
operator_not
operator_or
operator_random
operator_round
operator_subtract
pen_changePenColorParamBy
pen_changePenHueBy
pen_changePenShadeBy
pen_changePenSizeBy
pen_clear
pen_penDown
pen_penUp
pen_setPenColorParamTo
pen_setPenColorToColor
pen_setPenHueToNumber
pen_setPenShadeToNumber
pen_setPenSizeTo
pen_stamp
procedures_call
procedures_definition
sensing_answer
sensing_askandwait
sensing_coloristouchingcolor
sensing_current
sensing_dayssince2000
sensing_distanceto
sensing_keypressed
sensing_loud
sensing_loudness
sensing_mousedown
sensing_mousex
sensing_mousey
sensing_of
sensing_resettimer
sensing_setdragmode
sensing_timer
sensing_touchingcolor
sensing_touchingobject
sensing_userid
sensing_username
sound_beats_menu
sound_changeeffectby
sound_changevolumeby
sound_cleareffects
sound_effects_menu
sound_play
sound_playuntildone
sound_seteffectto
sound_setvolumeto
sound_sounds_menu
sound_stopallsounds
sound_volume
speech2text_getSpeech
speech2text_listenAndWait
speech2text_whenIHearHat
text2speech_setLanguage
text2speech_setVoice
text2speech_speakAndWait
translate_getTranslate
translate_getViewerLanguage
videoSensing_setVideoTransparency
videoSensing_videoOn
videoSensing_videoToggle
videoSensing_whenMotionGreaterThan
wedo2_getDistance
wedo2_getTiltAngle
wedo2_isTilted
wedo2_motorOff
wedo2_motorOn
wedo2_motorOnFor
wedo2_playNoteFor
wedo2_setLightHue
wedo2_setMotorDirection
wedo2_startMotorPower
wedo2_whenDistance
wedo2_whenTilted
""".split()

# 用内置数据初始化；若同目录存在 hat_opcodes.json，会在此并入
ALL_OPCODES = (set(BUILTIN_ALL_OPCODES)
               | BUILTIN_EXTRA_OPCODES
               | FACESENSING_OPCODES)
BLOCK_META_HAT_COUNT = _load_block_meta()

if __name__ == "__main__":
    try:
        setup_console_encoding()
        print("=== scratch文件优化工具 (V0.7) ===")
        if BLOCK_META_HAT_COUNT is not None:
            print(f"[积木元数据] 内置数据 + hat_opcodes.json："
                  f"{len(HAT_OPCODES)} 个帽子 / {len(ALL_OPCODES)} 个已知积木")
        else:
            print(f"[积木元数据] 使用内置数据："
                  f"{len(HAT_OPCODES)} 个帽子 / {len(ALL_OPCODES)} 个已知积木")
        raw_input = input("拖入文件/多个文件/文件夹: \n> ").strip()

        if not raw_input:
            print("未输入任何内容!")
        else:
            # 解析输入的路径：带引号的按引号切（拖拽时 Windows 会自动加引号），
            # 其余按空白切（可以一次输入多个路径）
            paths = re.findall(r'"([^"]+)"|(\S+)', raw_input)
            valid_paths = [p[0] if p[0] else p[1] for p in paths]

            # 兜底：整行本身就是一个“真实存在”的路径
            # （例如带空格但没加引号 —— 手输 / 粘贴时很常见，别被空格拆坏）
            whole = raw_input.strip().strip('"')
            if whole and os.path.exists(whole) and whole not in valid_paths:
                valid_paths = [whole]

            # 获取用户配置
            operations = ask_operations()
            if not any(operations.values()):
                raise ValueError("未选择任何优化")
            EXT_ALLOW_NETWORK = ask_extension_network()
            add_suffix = ask_cleaned_suffix()
            level = get_compression_level()

            # 分类路径
            all_files_to_process = []
            temp_output_root = None

            for p in valid_paths:
                p = p.strip('"')
                if not os.path.exists(p):
                    print(f"警告: 路径不存在 '{p}'，已跳过。")
                    continue

                if os.path.isfile(p) and p.lower().endswith('.sb3'):
                    all_files_to_process.append(p)
                    # 临时输出目录设为该文件所在目录下的 optimized_output
                    if temp_output_root is None:
                        temp_output_root = os.path.join(os.path.dirname(p), "optimized_output")
                elif os.path.isdir(p):
                    # 如果是文件夹，找出里面所有sb3
                    files_in_dir = find_sb3_files(p)
                    if files_in_dir:
                        all_files_to_process.extend(files_in_dir)
                        output_root = os.path.join(p, "optimized_output")
                        os.makedirs(output_root, exist_ok=True)
                        process_batch(p, files_in_dir, output_root, level, add_suffix, operations)
                    else:
                        print(f"提示: 文件夹中未找到 .sb3 文件 '{p}'")
                else:
                    print(f"警告: 忽略非 .sb3 文件 '{p}'")

            # 处理收集到的独立文件
            if all_files_to_process:
                # 所有独立文件输出到第一个文件所在目录的 optimized_output
                if temp_output_root:
                    os.makedirs(temp_output_root, exist_ok=True)
                    common_root = os.path.dirname(all_files_to_process[0])
                    process_batch(common_root, all_files_to_process, temp_output_root, level, add_suffix, operations)

    except KeyboardInterrupt:
        print("\n\n用户取消操作")
    except Exception as e:
        print(f"错误: {e}")
    finally:
        input("\n按回车退出...")
