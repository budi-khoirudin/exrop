import code
import pickle
from itertools import combinations, chain
from triton import *
from Gadget import *
from RopChain import *

def initialize():
    ctx = TritonContext()
    ctx.setArchitecture(ARCH.X86_64)
    ctx.setMode(MODE.ALIGNED_MEMORY, True)
    ctx.setAstRepresentationMode(AST_REPRESENTATION.PYTHON)
    return ctx

def findCandidatesWriteGadgets(gadgets, avoid_char=None):
    candidates = {}
    for gadget in list(gadgets):
        badchar = False
        if avoid_char:
            for char in avoid_char:
                addrb = gadget.addr.to_bytes(8, 'little')
                if char in addrb:
                    badchar = True
                    break
        if badchar:
            continue
        if gadget.is_memory_write:
            isw = gadget.is_memory_write
            if not isw in candidates:
                candidates[isw] = [gadget]
                continue
            candidates[isw].append(gadget)
    return candidates

def findForRet(gadgets, min_diff_sp=0, not_write_regs=set(), avoid_char=None):
    for gadget in list(gadgets):
        badchar = False
        if avoid_char:
            for char in avoid_char:
                addrb = gadget.addr.to_bytes(8, 'little')
                if char in addrb:
                    badchar = True
                    break
        if badchar:
            continue
        if set.intersection(not_write_regs, gadget.written_regs):
            continue
        if not gadget.is_memory_write and not gadget.is_memory_write and gadget.end_type == TYPE_RETURN and gadget.diff_sp == min_diff_sp:
            return gadget

def findPivot(gadgets, not_write_regs=set(), avoid_char=None):
    candidates = []
    for gadget in list(gadgets):
        badchar = False
        if avoid_char:
            for char in avoid_char:
                addrb = gadget.addr.to_bytes(8, 'little')
                if char in addrb:
                    badchar = True
                    break
        if badchar:
            continue
        if set.intersection(not_write_regs, gadget.written_regs):
            continue
        if gadget.pivot:
            candidates.append(gadget)
    return candidates

def findCandidatesGadgets(gadgets, regs_write, regs_items, not_write_regs=set(), avoid_char=None):
    candidates_pop = []
    candidates_write = []
    candidates_depends = []
    candidates_defined = []
    candidates_defined2 = []
    candidates_no_return = []
    candidates_for_ret = []
    depends_regs = set()
    for gadget in list(gadgets):
        if set.intersection(not_write_regs, gadget.written_regs) or gadget.is_memory_read or gadget.is_memory_write or gadget.end_type == TYPE_UNKNOWN:
            gadgets.remove(gadget)
            continue
        badchar = False
        if avoid_char:
            for char in avoid_char:
                addrb = gadget.addr.to_bytes(8, 'little')
                if char in addrb:
                    badchar = True
                    break
        if badchar:
            continue

        if gadget.end_type != TYPE_RETURN:
            if gadget.end_type == TYPE_JMP_REG or gadget.end_type == TYPE_CALL_REG:
                depends_regs.update(gadget.depends_regs)
                candidates_no_return.append(gadget)
            gadgets.remove(gadget)
            continue

        if set.intersection(regs_write,set(gadget.defined_regs.keys())):
            if regs_items and set.intersection(regs_items, set(gadget.defined_regs.items())):
                candidates_defined2.append(gadget)
            else:
                candidates_defined.append(gadget)
            gadgets.remove(gadget)
            depends_regs.update(gadget.depends_regs)
            continue

        if set.intersection(regs_write,gadget.popped_regs):
            candidates_pop.append(gadget)
            gadgets.remove(gadget)
            depends_regs.update(gadget.depends_regs)
            continue

        if set.intersection(regs_write,gadget.written_regs):
            candidates_write.append(gadget)
            gadgets.remove(gadget)
            depends_regs.update(gadget.depends_regs)
            continue

    if depends_regs:
        candidates_depends = findCandidatesGadgets(gadgets, depends_regs, set(), not_write_regs)
    candidates = candidates_defined2 + candidates_pop + candidates_defined + candidates_write + candidates_no_return + candidates_depends  # ordered by useful gadgets

    for gadget in gadgets:
        if gadget.diff_sp in [8,0]:
            candidates_for_ret.append(gadget)
            gadgets.remove(gadget)

    candidates += candidates_for_ret
    return candidates

def extract_byte(bv, pos):
    return (bv >> pos*8) & 0xff

def filter_byte(astctxt, bv, bc, bsize):
    nbv = []
    for i in range(bsize):
        nbv.append(astctxt.lnot(astctxt.equal(astctxt.extract(i*8+7, i*8, bv),astctxt.bv(bc, 8))))
    return nbv

def solveGadgets(gadgets, solves, avoid_char=None, keep_regs=set(), add_type=dict(), for_refind=set()):
    regs = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]
    candidates = findCandidatesGadgets(gadgets, set(solves.keys()), set(solves.items()), avoid_char=avoid_char, not_write_regs=keep_regs)
    ctx = initialize()
    astCtxt = ctx.getAstContext()
    chains = RopChain()

    for gadget in candidates:
        tmp_solved_ordered = []
        tmp_solved_regs = []
        if not gadget.is_asted:
            gadget.buildAst()
        reg_to_reg_solve = set()
        for reg,val in solves.items():
            if reg not in gadget.written_regs or reg in gadget.end_reg_used:
                continue

            regAst = gadget.regAst[reg]
            if reg in gadget.defined_regs and gadget.defined_regs[reg] == val:
                tmp_solved_regs.append(reg)
                tmp_solved_ordered.append([])
                if isinstance(val, str):
                    reg_to_reg_solve.add(val)
                continue

            refind_dict = {}
            if isinstance(val, str): # probably registers
                if reg in gadget.defined_regs and isinstance(gadget.defined_regs[reg], str) and gadget.defined_regs[reg] != reg:
                    refind_dict[gadget.defined_regs[reg]] = val
                    hasil = []
                else:
                    continue
            else:
                if avoid_char:
                    simpl = ctx.simplify(regAst, True)
                    childs = simpl.getChildren()
                    if not childs:
                        childs = [simpl]
                    filterbyte = []
                    lval = len(val.to_bytes(8, 'little').rstrip(b"\x00"))
                    hasil = False
                    for child in childs:
                        for char in avoid_char:
                            fb = filter_byte(astCtxt, child, char, lval)
                            filterbyte.extend(fb)
                    if filterbyte:
                        filterbyte.append(regAst == astCtxt.bv(val,64))
                        filterbyte = astCtxt.land(filterbyte)
                        hasil = list(ctx.getModel(filterbyte).values())
                    if not hasil: # try to find again
                        hasil = list(ctx.getModel(regAst == astCtxt.bv(val,64)).values())

                else:
                    hasil = list(ctx.getModel(regAst == astCtxt.bv(val,64)).values())

            for v in hasil:
                alias = v.getVariable().getAlias()
                if 'STACK' not in alias: # check if value is found not in stack
                    if alias in regs and alias not in refind_dict: # check if value is found in reg
                        if (alias != reg and alias not in for_refind) or v.getValue() != val:
                            refind_dict[alias] = v.getValue() # re-search value with new reg
                        else:
                            hasil = False
                            refind_dict = {}
                            break
                    else:
                        hasil = False
                        break
                elif avoid_char: # check if stack is popped contain avoid char
                    for char in avoid_char:
                        if char in val.to_bytes(8, 'little'):
                            hasil = False
                            refind_dict = False
                            break
            if refind_dict:
                tmp_for_refind = for_refind.copy() # don't overwrite old value
                tmp_for_refind.add(reg)
                hasil = solveGadgets(candidates[:], refind_dict, avoid_char, for_refind=tmp_for_refind)

            if hasil:
                if isinstance(val, str):
                    reg_to_reg_solve.add(gadget.defined_regs[reg])
                if not isinstance(hasil, RopChain):
                    type_chain = CHAINITEM_TYPE_VALUE
                    if add_type and reg in add_type and add_type[reg] == CHAINITEM_TYPE_ADDR:
                        type_chain = CHAINITEM_TYPE_ADDR
                    hasil = ChainItem.parseFromModel(hasil, type_val=type_chain)
                tmp_solved_ordered.append(hasil)
                tmp_solved_regs.append(reg)

        if not tmp_solved_ordered:
            continue

        if gadget.end_type != TYPE_RETURN:
            if set.intersection(set(list(solves.keys())), gadget.end_reg_used):
                continue
            next_gadget = None
#            print("handling no return gadget")
            diff = 0
            if gadget.end_type == TYPE_JMP_REG:
                next_gadget = findForRet(candidates[:], 0, set(tmp_solved_regs), avoid_char=avoid_char)
            elif gadget.end_type == TYPE_CALL_REG:
                next_gadget = findForRet(candidates[:], 8, set(tmp_solved_regs), avoid_char=avoid_char)
                diff = 8
            if not next_gadget:
                continue
            gadget.end_gadget = next_gadget
            gadget.diff_sp += next_gadget.diff_sp - diff

            regAst = gadget.end_ast
            val = gadget.end_gadget.addr
            hasil = ctx.getModel(regAst == val).values()

            refind_dict = {}
            type_chains = {}
            for v in hasil:
                alias = v.getVariable().getAlias()
                if 'STACK' not in alias:
                    if alias in regs and alias not in refind_dict:
                        refind_dict[alias] = v.getValue()
                        type_chains[alias] = CHAINITEM_TYPE_ADDR
                    else:
                        hasil = False
                        break
                elif avoid_char: # check if stack is popped contain avoid char
                    for char in avoid_char:
                        if char in val.to_bytes(8, 'little'):
                            hasil = False
                            refind_dict = False
                            break
            if refind_dict:
                hasil = solveGadgets(candidates[:], refind_dict, avoid_char, add_type=type_chains, keep_regs=reg_to_reg_solve)
            if not hasil:
                continue
            tmp_solved_regs.append('rip')
            tmp_solved_ordered.append(hasil)

        tmp_chain = Chain()
        tmp_chain.set_solved(gadget, tmp_solved_ordered, tmp_solved_regs)

        if not chains.insert_chain(tmp_chain):
            continue # can't insert chain

        for reg in tmp_solved_regs:
            if reg != 'rip':
                del solves[reg]

        if not solves:
            return chains

    return []

def solveWriteGadgets(gadgets, solves, avoid_char=None):
    regs = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]
    final_solved = []
    candidates = findCandidatesWriteGadgets(gadgets[:], avoid_char=avoid_char)
    ctx = initialize()
    gwr = list(candidates.keys())
    chains = RopChain()
    gwr.sort()
    for w in gwr:
        for gadget in candidates[w]:
            if not gadget.is_asted:
                gadget.buildAst()
            for addr,val in list(solves.items())[:]:
                mem_ast = gadget.memory_write_ast[0]
                if mem_ast[1].getBitvectorSize() != 64:
                    break
                addrhasil = ctx.getModel(mem_ast[0] == addr).values()
                valhasil = ctx.getModel(mem_ast[1] == val).values()
                if not addrhasil or not valhasil:
                    break
                hasil = list(addrhasil) + list(valhasil)
                refind_dict = {}
#                code.interact(local=locals())
                for v in hasil:
                    alias = v.getVariable().getAlias()
                    if 'STACK' not in alias:
                        if alias in regs and alias not in refind_dict:
                            refind_dict[alias] = v.getValue()
                        else:
                            hasil = False
                            break
                if hasil and refind_dict:
                    hasil = solveGadgets(gadgets[:], refind_dict, avoid_char=avoid_char)
                if hasil:
                    del solves[addr]
                    chain = Chain()
                    chain.set_solved(gadget, [hasil])
                    chains.insert_chain(chain)
                    if not solves:
                        return chains

def solvePivot(gadgets, addr_pivot, avoid_char=None):
    regs = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"]
    candidates = findPivot(gadgets, avoid_char=avoid_char)
    ctx = initialize()
    chains = RopChain()
    for gadget in candidates:
        if not gadget.is_asted:
            gadget.buildAst()
        hasil = ctx.getModel(gadget.pivot_ast == addr_pivot).values()
        for v in hasil:
            alias = v.getVariable().getAlias()
            refind_dict = dict()
            if 'STACK' not in alias:
                if alias in regs and alias not in refind_dict:
                    refind_dict[alias] = v.getValue()
                else:
                    hasil = False
                    break
            else:
                idxchain = int(alias.replace("STACK", ""))
                new_diff_sp = (idxchain+1)*8
        if hasil and refind_dict:
            hasil = solveGadgets(gadgets[:], refind_dict, avoid_char=avoid_char)
            new_diff_sp = 0
        if not hasil:
            continue
        gadget.diff_sp = new_diff_sp
        chain = Chain()
        chain.set_solved(gadget, [hasil])
        chains.insert_chain(chain)
        return chains
