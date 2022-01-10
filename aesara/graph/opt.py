"""
Defines the base class for optimizations as well as a certain
amount of useful generic optimization tools.

"""
import abc
import contextlib
import copy
import functools
import inspect
import logging
import pdb
import sys
import time
import traceback
import warnings
from collections import UserList, defaultdict, deque
from collections.abc import Iterable
from functools import partial, reduce
from itertools import chain
from typing import Dict, List, Optional, Tuple, Union

import aesara
from aesara.configdefaults import config
from aesara.graph import destroyhandler as dh
from aesara.graph.basic import (
    Apply,
    Constant,
    Variable,
    applys_between,
    io_toposort,
    nodes_constructed,
    vars_between,
)
from aesara.graph.features import Feature, NodeFinder
from aesara.graph.fg import FunctionGraph, InconsistencyError
from aesara.graph.op import Op
from aesara.graph.utils import AssocList
from aesara.misc.ordered_set import OrderedSet
from aesara.raise_op import CheckAndRaise
from aesara.utils import flatten


_logger = logging.getLogger("aesara.graph.opt")


class LocalMetaOptimizerSkipAssertionError(AssertionError):
    """This is an AssertionError, but instead of having the
    LocalMetaOptimizer print the error, it just skip that
    compilation.

    """


class Rewriter(abc.ABC):
    """Abstract base class for graph/term rewriters."""

    @abc.abstractmethod
    def add_requirements(self, fgraph: FunctionGraph):
        r"""Add `Feature`\s and other requirements to a `FunctionGraph`."""

    @abc.abstractmethod
    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        """Print a single-line, indented representation of the rewriter."""

    def __eq__(self, other):
        return self is other

    def __hash__(self):
        return id(self)


class GlobalOptimizer(Rewriter):
    """A optimizer that can be applied to a `FunctionGraph` in order to transform it.

    It can represent an optimization or, in general, any kind of transformation
    one could apply to a `FunctionGraph`.

    """

    @abc.abstractmethod
    def apply(self, fgraph):
        """Apply the optimization to a `FunctionGraph`.

        It may use all the methods defined by the `FunctionGraph`. If the
        `GlobalOptimizer` needs to use a certain tool, such as an
        `InstanceFinder`, it can do so in its `add_requirements` method.

        """
        raise NotImplementedError()

    def optimize(self, fgraph, *args, **kwargs):
        """

        This is meant as a shortcut for the following::

            opt.add_requirements(fgraph)
            opt.apply(fgraph)

        """
        self.add_requirements(fgraph)
        ret = self.apply(fgraph, *args, **kwargs)
        return ret

    def __call__(self, fgraph):
        """Optimize a `FunctionGraph`.

        This is the same as ``self.optimize(fgraph)``.

        """
        return self.optimize(fgraph)

    def add_requirements(self, fgraph):
        ...

    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        name = getattr(self, "name", None)
        print(
            f"{' ' * level}{self.__class__.__name__} {name} id={id(self)}",
            file=stream,
        )

    @staticmethod
    def print_profile(stream, prof, level=0):
        if prof is not None:
            raise NotImplementedError(
                "The function print_profile must be overridden if the"
                " optimizer return profiling information."
            )


class LocalOptimizer(Rewriter):
    """A node-based optimizer."""

    def tracks(self):
        """Return the list of `Op` classes to which this optimization applies.

        Returns ``None`` when the optimization applies to all nodes.

        """
        return None

    @abc.abstractmethod
    def transform(
        self, fgraph: FunctionGraph, node: Apply, *args, **kwargs
    ) -> Union[bool, List[Variable], Dict[Variable, Variable]]:
        r"""Transform a subgraph whose output is `node`.

        Subclasses should implement this function so that it returns one of the
        following:

        - ``False`` to indicate that no optimization can be applied to this `node`;
        - A list of `Variable`\s to use in place of the `node`'s current outputs.
        - A ``dict`` mapping old `Variable`\s to `Variable`\s.


        Parameters
        ----------
        fgraph :
            A `FunctionGraph` containing `node`.
        node :
            An `Apply` node to be transformed.

        """

        raise NotImplementedError()

    def add_requirements(self, fgraph):
        r"""Add required `Feature`\s to `fgraph`."""

    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        print(f"{' ' * level}{self.__class__.__name__} id={id(self)}", file=stream)


class FromFunctionOptimizer(GlobalOptimizer):
    """A `GlobalOptimizer` constructed from a given function."""

    def __init__(self, fn, requirements=()):
        self.fn = fn
        self.requirements = requirements

    def apply(self, *args, **kwargs):
        return self.fn(*args, **kwargs)

    def add_requirements(self, fgraph):
        for req in self.requirements:
            req(fgraph)

    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        print(f"{' ' * level}{self.apply} id={id(self)}", file=stream)

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)

    def __str__(self):
        return self.__name__


def optimizer(f):
    """Decorator for `FromFunctionOptimizer`."""
    rval = FromFunctionOptimizer(f)
    rval.__name__ = f.__name__
    return rval


def inplace_optimizer(f):
    """Decorator for `FromFunctionOptimizer` that also adds the `DestroyHandler` features."""
    dh_handler = dh.DestroyHandler
    requirements = (lambda fgraph: fgraph.attach_feature(dh_handler()),)
    rval = FromFunctionOptimizer(f, requirements)
    rval.__name__ = f.__name__
    return rval


class SeqOptimizer(GlobalOptimizer, UserList):
    """A `GlobalOptimizer` that applies a list of optimizers sequentially."""

    @staticmethod
    def warn(exc, self, optimizer):
        """Default ``failure_callback`` for `SeqOptimizer`."""
        _logger.error(f"SeqOptimizer apply {optimizer}")
        _logger.error("Traceback:")
        _logger.error(traceback.format_exc())
        if config.on_opt_error == "raise":
            raise exc
        elif config.on_opt_error == "pdb":
            pdb.post_mortem(sys.exc_info()[2])

    def __init__(self, *opts, failure_callback=None):
        """
        Parameters
        ----------
        *opts :
            The List of optimizers to be applied to a node
        failure_callback : callable or None
            Keyword only argument. A callback used when a failure
            happen during optimization.

        """
        if len(opts) == 1 and isinstance(opts[0], (list, tuple)):
            opts = opts[0]

        super().__init__(opts)

        self.failure_callback = failure_callback

    def apply(self, fgraph):
        """Applies each `GlobalOptimizer` in ``self.data`` to `fgraph`."""
        l = []
        if fgraph.profile:
            validate_before = fgraph.profile.validate_time
            sub_validate_time = [validate_before]
            callbacks_before = fgraph.execute_callbacks_times.copy()
        else:
            sub_validate_time = []
            callbacks_before = []
        callback_before = fgraph.execute_callbacks_time
        nb_node_before = len(fgraph.apply_nodes)
        sub_profs = []
        nb_nodes = []

        self.pre_profile = (
            self,
            l,
            -1,
            -1,
            nb_node_before,
            -1,
            sub_profs,
            sub_validate_time,
            nb_nodes,
            {},
        )
        try:
            for optimizer in self.data:
                try:
                    nb_nodes_before = len(fgraph.apply_nodes)
                    t0 = time.time()
                    sub_prof = optimizer.optimize(fgraph)
                    l.append(float(time.time() - t0))
                    sub_profs.append(sub_prof)
                    nb_nodes.append((nb_nodes_before, len(fgraph.apply_nodes)))
                    if fgraph.profile:
                        sub_validate_time.append(fgraph.profile.validate_time)
                except AssertionError:
                    # do not catch Assertion failures
                    raise
                except Exception as e:
                    if self.failure_callback:
                        self.failure_callback(e, self, optimizer)
                        continue
                    else:
                        raise
        finally:

            if fgraph.profile:
                validate_time = fgraph.profile.validate_time - validate_before
                callbacks_time = {}
                for k, v in fgraph.execute_callbacks_times.items():
                    if k in callbacks_before:
                        t = v - callbacks_before[k]
                        if t > 0:
                            callbacks_time[k] = t
                    else:
                        callbacks_time[k] = v
            else:
                validate_time = None
                callbacks_time = {}
            callback_time = fgraph.execute_callbacks_time - callback_before
            self.pre_profile = (
                self,
                l,
                validate_time,
                callback_time,
                nb_node_before,
                len(fgraph.apply_nodes),
                sub_profs,
                sub_validate_time,
                nb_nodes,
                callbacks_time,
            )
        return self.pre_profile

    def __repr__(self):
        return f"SeqOpt({self.data})"

    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        name = getattr(self, "name", None)
        print(
            f"{' ' * level}{self.__class__.__name__} {name} id={id(self)}", file=stream
        )
        # This way, -1 will do all depth
        if depth != 0:
            depth -= 1
            for opt in self.data:
                opt.print_summary(stream, level=(level + 2), depth=depth)

    @staticmethod
    def print_profile(stream, prof, level=0):
        (
            opts,
            prof,
            validate_time,
            callback_time,
            nb_node_before,
            nb_node_after,
            sub_profs,
            sub_validate_time,
            nb_nodes,
            callbacks_time,
        ) = prof
        blanc = "    " * level

        print(blanc, "SeqOptimizer", end=" ", file=stream)
        if hasattr(opts, "name"):
            print(blanc, opts.name, end=" ", file=stream)
        elif hasattr(opts, "__name__"):
            print(blanc, opts.__name__, end=" ", file=stream)
        print(
            (
                f" time {sum(prof):.3f}s for {int(nb_node_before)}/{int(nb_node_after)} nodes"
                " before/after optimization"
            ),
            file=stream,
        )
        print(blanc, f"  {callback_time:.3f}s for callback", file=stream)
        print(blanc, f"      {validate_time:.3f}s for fgraph.validate()", file=stream)
        if callback_time > 1:
            print(blanc, "  callbacks_time", file=stream)
            for i in sorted(callbacks_time.items(), key=lambda a: -a[1]):
                if i[1] > 0:
                    # We want to have the __str__ called, so we can't
                    # just print i.
                    print(blanc, "      ", i[0], ",", i[1], file=stream)

        if level == 0:
            print(
                blanc,
                "  time      - (name, class, index, nodes before, nodes after) - validate time",
                file=stream,
            )
        ll = []
        for (opt, nb_n) in zip(opts, nb_nodes):
            if hasattr(opt, "__name__"):
                name = opt.__name__
            else:
                name = opt.name
            idx = opts.index(opt)
            ll.append((name, opt.__class__.__name__, idx) + nb_n)
        lll = sorted(zip(prof, ll), key=lambda a: a[0])

        for (t, opt) in lll[::-1]:
            i = opt[2]
            if sub_validate_time:
                val_time = sub_validate_time[i + 1] - sub_validate_time[i]
                print(
                    blanc,
                    f"  {t:.6f}s - {opt} - {val_time:.3f}s",
                    file=stream,
                )
            else:
                print(blanc, f"  {t:.6f}s - {opt}", file=stream)

            if sub_profs[i]:
                opts[i].print_profile(stream, sub_profs[i], level=level + 1)
        print(file=stream)

    @staticmethod
    def merge_profile(prof1, prof2):
        """Merge two profiles."""
        new_t = []  # the time for the optimization
        new_l = []  # the optimization
        new_sub_profile = []
        # merge common(same object) opt
        for l in set(prof1[0]).intersection(set(prof2[0])):
            idx1 = prof1[0].index(l)
            idx2 = prof2[0].index(l)
            new_t.append(prof1[1][idx1] + prof2[1][idx2])
            new_l.append(l)
            if hasattr(l, "merge_profile"):
                assert len(prof1[6][idx1]) == len(prof2[6][idx2])
                new_sub_profile.append(l.merge_profile(prof1[6][idx1], prof2[6][idx2]))
            else:
                new_sub_profile.append(None)

        # merge not common opt
        from io import StringIO

        for l in set(prof1[0]).symmetric_difference(set(prof2[0])):
            # The set trick above only work for the same object optimization
            # It don't work for equivalent optimization.
            # So we try to merge equivalent optimization here.
            new_l_names = [o.name for o in new_l]
            if l.name in new_l_names:
                idx = new_l_names.index(l.name)
                io1 = StringIO()
                io2 = StringIO()
                l.print_summary(io1)
                new_l[idx].print_summary(io2)
                if io1.read() == io2.read():
                    if l in prof1[0]:
                        p = prof1
                    else:
                        p = prof2
                    new_t[idx] += p[1][p[0].index(l)]
                    if hasattr(l, "merge_profile"):
                        assert len(p[6][p[0].index(l)]) == len(new_sub_profile[idx])
                        new_sub_profile[idx] = l.merge_profile(
                            new_sub_profile[idx], p[6][p[0].index(l)]
                        )
                    else:
                        new_sub_profile[idx] = None
                continue
            if l in prof1[0]:
                p = prof1
            else:
                p = prof2
            new_t.append(p[1][p[0].index(l)])
            idx = p[0].index(l)
            new_l.append(l)
            new_sub_profile.append(p[6][idx])

        new_opt = SeqOptimizer(*new_l)
        new_nb_nodes = []
        for p1, p2 in zip(prof1[8], prof2[8]):
            new_nb_nodes.append((p1[0] + p2[0], p1[1] + p2[1]))
        new_nb_nodes.extend(prof1[8][len(new_nb_nodes) :])
        new_nb_nodes.extend(prof2[8][len(new_nb_nodes) :])

        new_callbacks_times = merge_dict(prof1[9], prof2[9])
        # We need to assert based on the name as we merge also based on
        # the name.
        assert {l.name for l in prof1[0]}.issubset({l.name for l in new_l})
        assert {l.name for l in prof2[0]}.issubset({l.name for l in new_l})
        assert len(new_t) == len(new_opt) == len(new_sub_profile)
        return (
            new_opt,
            new_t,
            prof1[2] + prof2[2],
            prof1[3] + prof2[3],
            -1,
            -1,
            new_sub_profile,
            [],
            new_nb_nodes,
            new_callbacks_times,
        )


class MergeFeature(Feature):
    """Keeps track of variables in a `FunctionGraph` that cannot be merged together.

    That way, the `MergeOptimizer` can remember the result of the last
    merge-pass on the `FunctionGraph`.

    """

    def on_attach(self, fgraph):
        assert not hasattr(fgraph, "merge_feature")
        fgraph.merge_feature = self

        # For constants
        self.seen_constants = set()
        # variable -> signature (for constants)
        self.const_sig = AssocList()
        # signature -> variable (for constants)
        self.const_sig_inv = AssocList()

        # For all Apply nodes
        # Set of distinct (not mergeable) nodes
        self.nodes_seen = set()
        # Ordered set of distinct (not mergeable) nodes without any input
        self.noinput_nodes = OrderedSet()

        # Each element of scheduled is a list of list of (out, new_out) pairs.
        # Each list of pairs represent the substitution needed to replace all
        # the outputs of a node with the outputs of a replacement candidate.
        # Each node can have several candidates. For instance, if "node" has
        # 2 outputs, and there are 3 replacement candidates, we will have:
        # shelf.scheduled = [
        #    [[(node.out1, cand1.out1), (node.out2, cand1.out2)],
        #     [(node.out1, cand2.out1), (node.out2, cand2.out2)],
        #     [(node.out1, cand3.out1), (node.out2, cand3.out2)]]]
        self.scheduled = []

        # List of (node, candidate) pairs, where we tried to replace node by
        # candidate, but it failed. This is used to avoid infinite loops
        # during the replacement phase.
        self.blacklist = []

        for node in fgraph.toposort():
            self.on_import(fgraph, node, "on_attach")

    def on_change_input(self, fgraph, node, i, r, new_r, reason):
        # If inputs to node change, it is not guaranteed that it is distinct
        # from the other nodes in nodes_seen
        if node in self.nodes_seen:
            self.nodes_seen.discard(node)
            self.process_node(fgraph, node)

        # Since we are in on_change_input, node should have inputs.
        if not isinstance(node, str):
            assert node.inputs

        if isinstance(new_r, Constant):
            self.process_constant(fgraph, new_r)

    def on_import(self, fgraph, node, reason):
        for c in node.inputs:
            if isinstance(c, Constant):
                self.process_constant(fgraph, c)

        self.process_node(fgraph, node)

    def on_prune(self, fgraph, node, reason):
        self.nodes_seen.discard(node)
        if not node.inputs:
            self.noinput_nodes.discard(node)
        for c in node.inputs:
            if isinstance(c, Constant) and (len(fgraph.clients[c]) <= 1):
                # This was the last node using this constant
                sig = self.const_sig[c]
                self.const_sig.discard(c)
                self.const_sig_inv.discard(sig)
                self.seen_constants.discard(id(c))

    def process_constant(self, fgraph, c):
        """Check if a constant `c` can be merged, and queue that replacement."""
        if id(c) in self.seen_constants:
            return
        sig = c.merge_signature()
        other_c = self.const_sig_inv.get(sig, None)
        if other_c is not None:
            # multiple names will clobber each other..
            # we adopt convention to keep the last name
            if c.name:
                other_c.name = c.name
            self.scheduled.append([[(c, other_c, "merge")]])
        else:
            # this is a new constant
            self.const_sig[c] = sig
            self.const_sig_inv[sig] = c
            self.seen_constants.add(id(c))

    def process_node(self, fgraph, node):
        """Check if a `node` can be merged, and queue that replacement."""
        if node in self.nodes_seen:
            return

        node_has_assert = False

        # These asserts ensure that the fgraph has set the clients field
        # properly.
        # The clients should at least contain `node` itself!
        if node.inputs:
            # Take the smallest clients list. Some ops like elemwise
            # have optimization that put constant as the first inputs.
            # As constant have in general more clients than other type of nodes
            # using always inputs[0] make us look at more nodes.
            # Always pick the smallest clints list between inputs 0
            # and -1 speed up optimization.

            a_clients = fgraph.clients[node.inputs[0]]
            b_clients = fgraph.clients[node.inputs[-1]]
            if len(a_clients) < len(b_clients):
                clients = a_clients
            else:
                clients = b_clients
            assert len(clients) > 0

            merge_candidates = [c for c, i in clients if c in self.nodes_seen]

            # Put all clients of `CheckAndRaise` inputs (if exist) into
            # `merge_candidates`
            # TODO: Deactivated for now, because it can create cycles in a
            # graph.  (There is a second deactivation part below.)
            # for i in node.inputs:
            #     if i.owner and isinstance(i.owner.op, CheckAndRaise):
            #         node_has_assert = True
            #         i_clients = fgraph.clients[i.owner.inputs[0]]
            #         assert_clients = [c for (c, _) in i_clients if c in self.nodes_seen]
            #
            #         for idx in range(len(assert_clients)):
            #             client = assert_clients[idx]
            #             if isinstance(i.owner.op, CheckAndRaise):
            #                 o_clients = fgraph.clients[client.outputs[0]]
            #                 for c in o_clients:
            #                     if c[0] in self.nodes_seen:
            #                         assert_clients.append(c[0])
            #
            #         merge_candidates.extend(assert_clients)
        else:
            # If two nodes have no input, but perform the same operation,
            # they are not always constant-folded, so we want to merge them.
            # In that case, the candidates are all the nodes without inputs.
            merge_candidates = self.noinput_nodes

        replacement_candidates = []
        for candidate in merge_candidates:
            if candidate is node:
                continue
            if len(node.inputs) != len(candidate.inputs):
                continue

            cand_has_assert = False

            # Get input list of the candidate with assert removed
            cand_inputs_assert_removed = []
            # TODO: Deactivated while `CheckAndRaise` merging is disabled. (See
            # above and below.)
            # for i in candidate.inputs:
            #     if i.owner and isinstance(i.owner.op, CheckAndRaise):
            #         cand_has_assert = True
            #         cand_inputs_assert_removed.append(i.owner.inputs[0])
            #     else:
            #         cand_inputs_assert_removed.append(i)

            # TODO: Remove this when `CheckAndRaise` merging is
            # re-enabled. (See above.)  Without `CheckAndRaise` merging we can
            # still look for identical `CheckAndRaise`, so we should not treat
            # `CheckAndRaise`s separately for now.
            cand_inputs_assert_removed = candidate.inputs

            # Get input list of the node with assert removed
            # if node_has_assert:
            #     node_inputs_assert_removed = []
            #     for i in node.inputs:
            #         if i.owner and isinstance(i.owner.op, CheckAndRaise):
            #             node_inputs_assert_removed.append(i.owner.inputs[0])
            #         else:
            #             node_inputs_assert_removed.append(i)
            # else:
            node_inputs_assert_removed = node.inputs

            inputs_match = all(
                node_in is cand_in
                for node_in, cand_in in zip(
                    node_inputs_assert_removed, cand_inputs_assert_removed
                )
            )

            if inputs_match and node.op == candidate.op:
                if (node, candidate) in self.blacklist:
                    # They were already tried, and there was an error
                    continue

                # replace node with candidate
                if not node_has_assert and not cand_has_assert:
                    # Schedule transfer of clients from node to candidate
                    pairs = list(
                        zip(
                            node.outputs,
                            candidate.outputs,
                            ["merge"] * len(node.outputs),
                        )
                    )

                # # if the current node has assert input, it should not be
                # # replaced with a candidate node which has no assert input
                # elif node_has_assert and not cand_has_assert:
                #     pairs = list(
                #         zip(
                #             candidate.outputs,
                #             node.outputs,
                #             ["merge"] * len(node.outputs),
                #         )
                #     )
                # else:
                #     new_inputs = self.get_merged_assert_input(node, candidate)
                #     new_node = node.op(*new_inputs)
                #     pairs = list(
                #         zip(
                #             node.outputs,
                #             new_node.owner.outputs,
                #             ["new_node"] * len(node.outputs),
                #         )
                #     ) + list(
                #         zip(
                #             candidate.outputs,
                #             new_node.owner.outputs,
                #             ["new_node"] * len(node.outputs),
                #         )
                #     )

                # transfer names
                for pair in pairs:
                    node_output, cand_output = pair[:2]
                    # clobber old name with new one
                    # it's arbitrary... one of the names has to go
                    if node_output.name:
                        cand_output.name = node_output.name

                replacement_candidates.append(pairs)

        if replacement_candidates:
            self.scheduled.append(replacement_candidates)
        else:
            self.nodes_seen.add(node)
            if not node.inputs:
                self.noinput_nodes.add(node)

    # def get_merged_assert_input(self, node, candidate):
    #     new_inputs = []
    #     for node_i, cand_i in zip(node.inputs, candidate.inputs):
    #         if node_i.owner and isinstance(node_i.owner.op, CheckAndRaise):
    #             if (
    #                 cand_i.owner
    #                 and isinstance(cand_i.owner.op, CheckAndRaise)
    #                 and node_i.owner.op.exc_type == cand_i.owner.op.exc_type
    #             ):
    #                 # Here two assert nodes are merged.
    #                 # Step 1. Merge conditions of both assert nodes.
    #                 # Step 2. Make the new assert node
    #                 node_cond = node_i.owner.inputs[1:]
    #                 cand_cond = cand_i.owner.inputs[1:]
    #                 new_cond = list(set(node_cond + cand_cond))
    #                 new_raise_op = CheckAndRaise(
    #                     node_i.owner.op.exc_type,
    #                     "; ".join([node_i.owner.op.msg, cand_i.owner.op.msg]),
    #                 )
    #                 new_inputs.append(new_raise_op(*(node_i.owner.inputs[:1] + new_cond)))
    #
    #             # node_i is assert, cand_i is not assert
    #             else:
    #                 new_inputs.append(node_i)
    #         else:
    #             # if node_i is not an assert node, append cand_i
    #             new_inputs.append(cand_i)
    #
    #     return new_inputs


class MergeOptimizer(GlobalOptimizer):
    r"""Merges parts of the graph that are identical and redundant.

    The basic principle is that if two `Apply`\s have `Op`\s that compare equal, and
    identical inputs, then they do not both need to be computed. The clients of
    one are transferred to the other and one of them is removed from the graph.
    This procedure is carried out in input-to-output order throughout the graph.

    The first step of merging is constant-merging, so that all clients of an
    ``int(1)`` for example, are transferred to just one particular instance of
    ``int(1)``.

    """

    def add_requirements(self, fgraph):
        if not hasattr(fgraph, "merge_feature"):
            fgraph.attach_feature(MergeFeature())

    def apply(self, fgraph):
        # Constant and non-constant are now applied in the same phase.
        # I am not sure why, but it seems to be faster this way.
        sched = fgraph.merge_feature.scheduled
        nb_fail = 0
        t0 = time.time()
        if fgraph.profile:
            validate_before = fgraph.profile.validate_time
            callback_before = fgraph.execute_callbacks_time
            callbacks_before = fgraph.execute_callbacks_times.copy()

        nb_merged = 0
        nb_constant = 0
        while sched:
            pairs_list = sched.pop()
            success = True
            for pairs_ in pairs_list:
                # We must check again the equivalence, as the graph
                # could've changed. If so, doing the replacement can
                # introduce a node that depends on itself.  Doing the
                # full check of such cycles every time is very time
                # consuming. I think this double check is faster than
                # doing the full cycle check. The full cycle check is
                # skipped by validate() if the graph doesn't contain
                # destroyers.
                var, candidate, merge_mode = pairs_[0]
                if merge_mode == "new_node" and var in fgraph.variables:
                    pass
                elif var not in fgraph.variables or candidate not in fgraph.variables:
                    continue

                # Keep len(item) == 2 for item in pairs
                pairs = [pair[:2] for pair in pairs_]

                if var.owner and candidate.owner:
                    node = var.owner
                    candidate = candidate.owner

                    # Get input list of the candidate node with assert
                    # nodes removed
                    cand_inputs_assert_removed = []
                    for i in candidate.inputs:
                        if i.owner and isinstance(i.owner.op, CheckAndRaise):
                            cand_inputs_assert_removed.append(i.owner.inputs[0])
                        else:
                            cand_inputs_assert_removed.append(i)

                    # Get input list of the node with assert nodes removed
                    node_inputs_assert_removed = []
                    for i in node.inputs:
                        if i.owner and isinstance(i.owner.op, CheckAndRaise):
                            node_inputs_assert_removed.append(i.owner.inputs[0])
                        else:
                            node_inputs_assert_removed.append(i)

                    if merge_mode == "new_node":
                        inputs_match = True
                    else:
                        inputs_match = all(
                            node_in is cand_in
                            for node_in, cand_in in zip(
                                node_inputs_assert_removed, cand_inputs_assert_removed
                            )
                        )

                    # No need to compare the op again, as it don't change.
                    if not inputs_match:
                        continue

                    if hasattr(fgraph, "destroy_handler"):
                        # If both nodes have clients that destroy them, we
                        # can't merge them.
                        clients = (
                            fgraph.clients[pairs[0][0]] + fgraph.clients[pairs[0][1]]
                        )
                        if (
                            sum(
                                [
                                    i in flatten(c.op.destroy_map.values())
                                    for c, i in clients
                                    if c != "output" and c.op.destroy_map
                                ]
                            )
                            > 1
                        ):
                            continue

                if len(pairs) == 1 and pairs[0][0].type != pairs[0][1].type:
                    res = pairs[0][0].type.convert_variable(pairs[0][1])

                    # Since the fgraph.replace only checks the convert_variable
                    # in one way, we change the order in the case that
                    # convert_variable will not be successful.
                    if not res:
                        pairs = [(pairs[0][1], pairs[0][0])]

                try:
                    # If all Constants, no need to call validate.
                    # Only need to check one of the var of each pairs.
                    # If it is a Constant, the other must also be a Constant as we merge them.
                    if all(isinstance(old, Constant) for old, new in pairs):
                        fgraph.replace_all(pairs, reason="MergeOptimizer")
                    else:
                        fgraph.replace_all_validate(pairs, reason="MergeOptimizer")
                except InconsistencyError:
                    success = False
                    nb_fail += 1
                    fgraph.merge_feature.blacklist.append(
                        (pairs[0][0].owner, pairs[0][1].owner)
                    )

                if success:
                    nb_merged += len(pairs)
                    if isinstance(pairs[0][0], Constant):
                        nb_constant += 1
                        # print pairs, pairs[0][0].type
                    break

        if fgraph.profile:
            validate_time = fgraph.profile.validate_time - validate_before
            callback_time = fgraph.execute_callbacks_time - callback_before
            callbacks_time = {}
            for k, v in fgraph.execute_callbacks_times.items():
                if k in callbacks_before:
                    t = v - callbacks_before[k]
                    if t > 0:
                        callbacks_time[k] = t
                else:
                    callbacks_time[k] = v
        else:
            validate_time = None
            callback_time = None
            callbacks_time = {}
        # clear blacklist
        fgraph.merge_feature.blacklist = []
        return (
            nb_fail,
            time.time() - t0,
            validate_time,
            callback_time,
            callbacks_time,
            nb_merged,
            nb_constant,
        )

    def __str__(self):
        return self.__class__.__name__

    @staticmethod
    def print_profile(stream, prof, level=0):

        (
            nb_fail,
            replace_time,
            validate_time,
            callback_time,
            callbacks_time,
            nb_merged,
            nb_constant,
        ) = prof

        blanc = "    " * level
        print(blanc, "MergeOptimizer", file=stream)
        print(
            blanc,
            f"  nb fail={nb_fail:5d} merged={nb_merged:5d} constant={nb_constant:5d}",
            file=stream,
        )
        print(
            blanc,
            f"  time replace={replace_time:2.2f} validate={validate_time:2.2f} callback={callback_time:2.2f}",
            file=stream,
        )
        if callback_time > 1:
            print(blanc, "  callbacks_time", file=stream)
            for i in sorted(callbacks_time.items(), key=lambda a: a[1]):
                if i[1] > 0:
                    # We want to have the __str__ called, so we can't
                    # just print i.
                    print(blanc, "      ", i[0], ",", i[1], file=stream)

    @staticmethod
    def merge_profile(prof1, prof2):
        def merge_none_number(v1, v2):
            if v1 is None:
                return v2
            if v2 is None:
                return v1
            return v1 + v2

        nb_fail = prof1[0] + prof2[0]
        replace_time = prof1[1] + prof2[1]
        validate_time = merge_none_number(prof1[2], prof2[2])
        callback_time = merge_none_number(prof1[3], prof2[3])
        callbacks_time = merge_dict(prof1[4], prof2[4])
        nb_merged = prof1[5] + prof2[5]
        nb_constant = prof1[6] + prof2[6]
        return (
            nb_fail,
            replace_time,
            validate_time,
            callback_time,
            callbacks_time,
            nb_merged,
            nb_constant,
        )


def pre_constant_merge(fgraph, variables):
    """Merge constants in the graphs given by `variables`.

    .. warning::

        This changes the nodes in a graph in-place!

    Parameters
    ----------
    fgraph
        A `FunctionGraph` instance in which some of these `variables` may
        reside.

        We want to avoid terms in `variables` that are contained in `fgraph`.
        The reason for that: it will break consistency of `fgraph` and its
        features (e.g. `ShapeFeature`).

    variables
        A list of nodes for which we want to merge constant inputs.

    Notes
    -----
    It is used to pre-merge nodes generated inside an optimization.  It is
    useful if there are many such replacements to make, so that `DebugMode`
    will not check each of them.

    """
    seen_var = set()
    # signature -> variable (for constants)
    const_sig_inv = {}
    if isinstance(variables, Variable):
        variables = [variables]

    def recursive_merge(var):

        if var in seen_var:
            return var

        if not hasattr(var, "owner"):
            return var

        # We don't want to merge constants that are *within* the
        # `FunctionGraph`
        if var.owner in fgraph.apply_nodes:
            return var

        seen_var.add(var)

        if isinstance(var, Constant):
            sig = var.signature()

            if sig in const_sig_inv:
                return const_sig_inv[sig]

            const_sig_inv[sig] = var

            return var

        if var.owner:
            for idx, inp in enumerate(var.owner.inputs):
                # XXX: This is changing the graph in place!
                var.owner.inputs[idx] = recursive_merge(inp)
        return var

    return [recursive_merge(v) for v in variables]


class LocalMetaOptimizer(LocalOptimizer):
    r"""
    Base class for meta-optimizers that try a set of `LocalOptimizer`\s
    to replace a node and choose the one that executes the fastest.

    If the error ``LocalMetaOptimizerSkipAssertionError`` is raised during
    compilation, we will skip that function compilation and not print
    the error.

    """

    def __init__(self):
        self.verbose = config.metaopt__verbose
        self.track_dict = defaultdict(lambda: [])
        self.tag_dict = defaultdict(lambda: [])
        self._tracks = []
        self.optimizers = []

    def register(self, optimizer, tag_list):
        self.optimizers.append(optimizer)
        for c in optimizer.tracks():
            self.track_dict[c].append(optimizer)
            self._tracks.append(c)
        for tag in tag_list:
            self.tag_dict[tag].append(optimizer)

    def tracks(self):
        return self._tracks

    def transform(self, fgraph, node, *args, **kwargs):
        # safety check: depending on registration, tracks may have been ignored
        if self._tracks is not None:
            if not isinstance(node.op, tuple(self._tracks)):
                return
        # first, we need to provide dummy values for all inputs
        # to the node that are not shared variables anyway
        givens = {}
        missing = set()
        for input in node.inputs:
            if isinstance(input, aesara.compile.SharedVariable):
                pass
            elif hasattr(input.tag, "test_value"):
                givens[input] = aesara.shared(
                    input.type.filter(input.tag.test_value),
                    input.name,
                    broadcastable=input.broadcastable,
                    borrow=True,
                )
            else:
                missing.add(input)
        if missing:
            givens.update(self.provide_inputs(node, missing))
            missing.difference_update(givens.keys())
        # ensure we have data for all input variables that need it
        if missing:
            if self.verbose > 0:
                print(
                    f"{self.__class__.__name__} cannot meta-optimize {node}, "
                    f"{len(missing)} of {int(node.nin)} input shapes unknown"
                )
            return
        # now we can apply the different optimizations in turn,
        # compile the resulting subgraphs and time their execution
        if self.verbose > 1:
            print(
                f"{self.__class__.__name__} meta-optimizing {node} ({len(self.get_opts(node))} choices):"
            )
        timings = []
        for opt in self.get_opts(node):
            outputs = opt.transform(fgraph, node, *args, **kwargs)
            if outputs:
                try:
                    fn = aesara.function(
                        [], outputs, givens=givens, on_unused_input="ignore"
                    )
                    fn.trust_input = True
                    timing = min(self.time_call(fn) for _ in range(2))
                except LocalMetaOptimizerSkipAssertionError:
                    continue
                except Exception as e:
                    if self.verbose > 0:
                        print(f"* {opt}: exception", e)
                    continue
                else:
                    if self.verbose > 1:
                        print(f"* {opt}: {timing:.5g} sec")
                    timings.append((timing, outputs, opt))
            else:
                if self.verbose > 0:
                    print(f"* {opt}: not applicable")
        # finally, we choose the fastest one
        if timings:
            timings.sort()
            if self.verbose > 1:
                print(f"= {timings[0][2]}")
            return timings[0][1]
        return

    def provide_inputs(self, node, inputs):
        """Return a dictionary mapping some `inputs` to `SharedVariable` instances of with dummy values.

        The `node` argument can be inspected to infer required input shapes.

        """
        raise NotImplementedError()

    def get_opts(self, node):
        """Return the optimizations that apply to `node`.

        This uses ``self.track_dict[type(node.op)]`` by default.
        """
        return self.track_dict[type(node.op)]

    def time_call(self, fn):
        start = time.time()
        fn()
        return time.time() - start


class FromFunctionLocalOptimizer(LocalOptimizer):
    """A `LocalOptimizer` constructed from a function."""

    def __init__(self, fn, tracks=None, requirements=()):
        self.fn = fn
        self._tracks = tracks
        self._tracked_types = (
            tuple(t for t in tracks if isinstance(t, type)) if tracks else ()
        )
        self.requirements = requirements

    def transform(self, fgraph, node):
        if self._tracks:
            if not (
                node.op in self._tracks or isinstance(node.op, self._tracked_types)
            ):
                return False

        return self.fn(fgraph, node)

    def add_requirements(self, fgraph):
        for req in self.requirements:
            req(fgraph)

    def tracks(self):
        return self._tracks

    def __str__(self):
        return getattr(self, "__name__", repr(self))

    def __repr__(self):
        return f"FromFunctionLocalOptimizer({repr(self.fn)}, {repr(self._tracks)}, {repr(self.requirements)})"

    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        print(f"{' ' * level}{self.transform} id={id(self)}", file=stream)


def local_optimizer(
    tracks: Optional[List[Union[Op, type]]],
    inplace: bool = False,
    requirements: Optional[Tuple[type, ...]] = (),
):
    r"""A decorator used to construct `FromFunctionLocalOptimizer` instances.

    Parameters
    ----------
    tracks
        The `Op` types or instances to which this optimization applies.
        Use ``None`` instead of an empty list to have the optimization apply to
        all `Op`s`.
    inplace
        A boolean indicating whether or not the optimization works in-place.
        If ``True``, a `DestroyHandler` `Feature` is added automatically added
        to the `FunctionGraph`\s applied to this optimization.
    requirements
        `Feature` types required by this optimization.

    """

    if requirements is None:
        requirements = ()

    def decorator(f):
        if tracks is not None:
            if len(tracks) == 0:
                raise ValueError(
                    "Use `None` instead of an empty list to make an optimization apply to all nodes."
                )
            for t in tracks:
                if not (
                    isinstance(t, Op) or (isinstance(t, type) and issubclass(t, Op))
                ):
                    raise TypeError(
                        "`tracks` must consist of `Op` classes or instances."
                    )
        req = requirements
        if inplace:
            dh_handler = dh.DestroyHandler
            req = tuple(requirements) + (
                lambda fgraph: fgraph.attach_feature(dh_handler()),
            )
        rval = FromFunctionLocalOptimizer(f, tracks, req)
        rval.__name__ = f.__name__
        return rval

    return decorator


class LocalOptTracker:
    r"""A container that maps rewrites to `Op` instances and `Op`-type inheritance."""

    def __init__(self):
        self.tracked_instances = {}
        self.tracked_types = {}
        self.untracked_opts = []

    def add_tracker(self, rw: LocalOptimizer):
        """Add a `LocalOptimizer` to be keyed by its `LocalOptimizer.tracks` or applied generally."""
        tracks = rw.tracks()

        if tracks is None:
            self.untracked_opts.append(rw)
        else:
            for c in tracks:
                if isinstance(c, type):
                    self.tracked_types.setdefault(c, []).append(rw)
                else:
                    self.tracked_instances.setdefault(c, []).append(rw)

    def _find_impl(self, cls):
        r"""Returns the `LocalOptimizer`\s that apply to `cls` based on inheritance.

        This based on `functools._find_impl`.
        """
        mro = functools._compose_mro(cls, self.tracked_types.keys())
        matches = []
        for t in mro:
            match = self.tracked_types.get(t, None)
            if match:
                matches.extend(match)
        return matches

    @functools.lru_cache()
    def get_trackers(self, op: Op) -> List[LocalOptimizer]:
        """Get all the rewrites applicable to `op`."""
        return (
            self._find_impl(type(op))
            + self.tracked_instances.get(op, [])
            + self.untracked_opts
        )

    def get_rewriters(self):
        return chain(
            chain.from_iterable(
                chain(self.tracked_types.values(), self.tracked_instances.values())
            ),
            self.untracked_opts,
        )


class LocalOptGroup(LocalOptimizer):
    r"""An optimizer that applies a list of `LocalOptimizer`\s to a node.

    Attributes
    ----------
    reentrant : bool
        Some global optimizers, like `NavigatorOptimizer`, use this value to
        determine if they should ignore new nodes.
    retains_inputs : bool
        States whether or not the inputs of a transformed node are transferred
        to the outputs.
    """

    def __init__(
        self, *optimizers, apply_all_opts: bool = False, profile: bool = False
    ):
        """

        Parameters
        ----------
        optimizers
            A list of optimizers to be applied to nodes.
        apply_all_opts
            If ``False``, it will return after the first successfully applied
            rewrite; otherwise, it will apply every applicable rewrite
            incrementally.
        profile
            Whether or not to profile the optimizations.

        """
        super().__init__()

        if len(optimizers) == 1 and isinstance(optimizers[0], list):
            # This happen when created by LocalGroupDB.
            optimizers = tuple(optimizers[0])
        self.opts = optimizers
        assert isinstance(self.opts, tuple)

        self.reentrant = any(getattr(opt, "reentrant", True) for opt in optimizers)
        self.retains_inputs = all(
            getattr(opt, "retains_inputs", False) for opt in optimizers
        )

        self.apply_all_opts = apply_all_opts

        self.profile = profile
        if self.profile:
            self.time_opts = {}
            self.process_count = {}
            self.applied_true = {}
            self.node_created = {}

        self.tracker = LocalOptTracker()

        for o in self.opts:

            self.tracker.add_tracker(o)

            if self.profile:
                self.time_opts.setdefault(o, 0)
                self.process_count.setdefault(o, 0)
                self.applied_true.setdefault(o, 0)
                self.node_created.setdefault(o, 0)

    def __str__(self):
        return getattr(
            self,
            "__name__",
            f"LocalOptGroup({','.join([str(o) for o in self.opts])})",
        )

    def tracks(self):
        t = []
        for l in self.opts:
            at = l.tracks()
            if at:
                t.extend(at)
        return t

    def transform(self, fgraph, node):
        if len(self.opts) == 0:
            return

        repl = None

        while True:
            opts = self.tracker.get_trackers(node.op)

            new_repl = None
            for opt in opts:
                opt_start = time.time()
                new_repl = opt.transform(fgraph, node)
                opt_finish = time.time()
                if self.profile:
                    self.time_opts[opt] += opt_start - opt_finish
                    self.process_count[opt] += 1
                if not new_repl:
                    continue
                if isinstance(new_repl, (tuple, list)):
                    new_vars = new_repl
                else:  # It must be a dict
                    new_vars = list(new_repl.values())

                if config.optimizer_verbose:
                    print(f"optimizer: rewrite {opt} replaces {node} with {new_repl}")

                if self.profile:
                    self.node_created[opt] += len(
                        list(applys_between(fgraph.variables, new_vars))
                    )
                    self.applied_true[opt] += 1
                break  # break from the for loop over optimization.
            if not new_repl:  # No optimization applied in the last iteration
                return repl
            # only 1 iteration
            if not self.apply_all_opts:
                return new_repl
            if not new_vars[0].owner:
                # We are at the start of the graph.
                return new_repl
            if len(new_repl) > 1:
                s = {v.owner for v in new_repl}
                assert len(s) == 1
            repl = new_repl
            node = new_vars[0].owner

    @staticmethod
    def print_profile(stream, prof, level=0):
        (time_opts, process_count, applied_true, node_created, profile) = prof

        if not profile:
            return

        blanc = "    " * int(level)
        print(blanc, "LocalOptGroup", file=stream)
        print(blanc, "---------------------", file=stream)
        count_opt = []
        not_used = []
        not_used_time = 0
        for o, count in process_count.items():
            if count > 0:
                count_opt.append(
                    (time_opts[o], applied_true[o], count, o, node_created[o])
                )
            else:
                not_used.append((time_opts[o], o))
                not_used_time += time_opts[o]
        if count_opt:
            print(
                blanc,
                "  time taken - times applied - times tried - name - node_created:",
                file=stream,
            )
            count_opt.sort()
            for (t, a_t, count, o, n_c) in count_opt[::-1]:
                print(
                    blanc,
                    f"  {t:.3f}s - {int(a_t)} - {int(count)} - {o} - {int(n_c)}",
                    file=stream,
                )
            print(
                blanc,
                f"  {not_used_time:.3f}s - in {len(not_used)} optimization that were not used (display those with runtime greater than 0)",
                file=stream,
            )
            not_used.sort(key=lambda nu: (nu[0], str(nu[1])))
            for (t, o) in not_used[::-1]:
                if t > 0:
                    # Skip opt that have 0 times, they probably wasn't even tried.
                    print(blanc + "  ", f"  {t:.3f}s - {o}", file=stream)
        else:
            print(blanc, " The optimizer wasn't successful ", file=stream)

        print(file=stream)

    def merge_profile(prof1, prof2):
        raise NotImplementedError

    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        print(f"{' ' * level}{self.__class__.__name__} id={id(self)}", file=stream)
        if depth != 0:
            depth -= 1
            for lopt in self.opts:
                lopt.print_summary(stream, level=(level + 2), depth=depth)

    def add_requirements(self, fgraph):
        for opt in self.opts:
            opt.add_requirements(fgraph)


class OpSub(LocalOptimizer):
    """

    Replaces the application of a certain `Op` by the application of
    another `Op` that takes the same inputs as what it is replacing.

    Parameters
    ----------
    op1, op2
        ``op1.make_node`` and ``op2.make_node`` must take the same number of
        inputs and have the same number of outputs.

    Examples
    --------

        OpSub(add, sub) ==>
            add(div(x, y), add(y, x)) -> sub(div(x, y), sub(y, x))

    """

    # an OpSub does not apply to the nodes it produces
    reentrant = False
    # all the inputs of the original node are transferred to the outputs
    retains_inputs = True

    def __init__(self, op1, op2, transfer_tags=True):
        self.op1 = op1
        self.op2 = op2
        self.transfer_tags = transfer_tags

    def op_key(self):
        return self.op1

    def tracks(self):
        return [self.op1]

    def transform(self, fgraph, node):
        if node.op != self.op1:
            return False
        repl = self.op2.make_node(*node.inputs)
        if self.transfer_tags:
            repl.tag = copy.copy(node.tag)
            for output, new_output in zip(node.outputs, repl.outputs):
                new_output.tag = copy.copy(output.tag)
        return repl.outputs

    def __str__(self):
        return f"{self.op1} -> {self.op2}"


class OpRemove(LocalOptimizer):
    """
    Removes all applications of an `Op` by transferring each of its
    outputs to the corresponding input.

    """

    reentrant = False  # no nodes are added at all

    def __init__(self, op):
        self.op = op

    def op_key(self):
        return self.op

    def tracks(self):
        return [self.op]

    def transform(self, fgraph, node):
        if node.op != self.op:
            return False
        return node.inputs

    def __str__(self):
        return f"{self.op}(x) -> x"

    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        print(
            f"{' ' * level}{self.__class__.__name__}(self.op) id={id(self)}",
            file=stream,
        )


class PatternSub(LocalOptimizer):
    """Replace all occurrences of an input pattern with an output pattern.

    The input and output patterns have the following syntax:

        input_pattern ::= (op, <sub_pattern1>, <sub_pattern2>, ...)
        input_pattern ::= dict(pattern = <input_pattern>,
                               constraint = <constraint>)
        sub_pattern ::= input_pattern
        sub_pattern ::= string
        sub_pattern ::= a Constant instance
        sub_pattern ::= int
        sub_pattern ::= float
        constraint ::= lambda fgraph, expr: additional matching condition

        output_pattern ::= (op, <output_pattern1>, <output_pattern2>, ...)
        output_pattern ::= string
        output_pattern ::= int
        output_pattern ::= float

    Each string in the input pattern is a variable that will be set to
    whatever expression is found in its place. If the same string is
    used more than once, the same expression must be found in those
    places. If a string used in the input pattern is used in the
    output pattern, the matching expression will be inserted in its
    place. The input pattern cannot just be a string but the output
    pattern can.

    If you put a constant variable in the input pattern, there will be a
    match iff a constant variable with the same value and the same type
    is found in its place.

    You can add a constraint to the match by using the ``dict(...)`` form
    described above with a ``'constraint'`` key. The constraint must be a
    function that takes the fgraph and the current Variable that we are
    trying to match and returns True or False according to an
    arbitrary criterion.

    The constructor creates a `PatternSub` that replaces occurrences of
    `in_pattern` by occurrences of `out_pattern`.

    Parameters
    ----------
    in_pattern :
        The input pattern that we want to replace.
    out_pattern :
        The replacement pattern.
    allow_multiple_clients : bool
        If False, the pattern matching will fail if one of the subpatterns has
        more than one client.
    skip_identities_fn : TODO
    name :
        Allows to override this optimizer name.
    tracks : optional
        The values that :meth:`self.tracks` will return. Useful to speed up
        optimization sometimes.
    get_nodes : optional
        If you provide `tracks`, you must provide this parameter. It must be a
        function that takes the tracked node and returns a list of nodes on
        which we will try this optimizer.

    Notes
    -----
    `tracks` and `get_nodes` can be used to make this optimizer track a less
    frequent `Op`, so this will make this optimizer tried less frequently.

    Examples
    --------

        PatternSub((add, 'x', 'y'), (add, 'y', 'x'))
        PatternSub((multiply, 'x', 'x'), (square, 'x'))
        PatternSub((subtract, (add, 'x', 'y'), 'y'), 'x')
        PatternSub((power, 'x', Constant(double, 2.0)), (square, 'x'))
        PatternSub((boggle, {'pattern': 'x',
                            'constraint': lambda expr: expr.type == scrabble}),
                   (scrabble, 'x'))

    """

    def __init__(
        self,
        in_pattern,
        out_pattern,
        allow_multiple_clients=False,
        skip_identities_fn=None,
        name=None,
        tracks=(),
        get_nodes=None,
        values_eq_approx=None,
    ):
        from aesara.graph.unify import convert_strs_to_vars

        var_map = {}
        self.in_pattern = convert_strs_to_vars(in_pattern, var_map=var_map)
        self.out_pattern = convert_strs_to_vars(out_pattern, var_map=var_map)
        self.values_eq_approx = values_eq_approx
        if isinstance(in_pattern, (list, tuple)):
            self.op = self.in_pattern[0]
        elif isinstance(in_pattern, dict):
            self.op = self.in_pattern["pattern"][0]
        else:
            raise TypeError(
                "The pattern to search for must start with a specific Op instance."
            )
        self.__doc__ = (
            self.__class__.__doc__ + "\n\nThis instance does: " + str(self) + "\n"
        )
        self.allow_multiple_clients = allow_multiple_clients
        self.skip_identities_fn = skip_identities_fn
        if name:
            self.__name__ = name
        self._tracks = tracks
        self.get_nodes = get_nodes
        if tracks != ():
            assert get_nodes

    def op_key(self):
        return self.op

    def tracks(self):
        if self._tracks != ():
            return self._tracks
        return [self.op]

    def transform(self, fgraph, node, get_nodes=True):
        """Check if the graph from node corresponds to ``in_pattern``.

        If it does, it constructs ``out_pattern`` and performs the replacement.

        """
        from etuples.core import ExpressionTuple
        from unification import reify, unify

        # TODO: We shouldn't need to iterate like this.
        if not self.allow_multiple_clients and any(
            len(fgraph.clients.get(v)) > 1
            for v in vars_between(fgraph.inputs, node.outputs)
            if v not in fgraph.inputs
        ):
            return False

        if get_nodes and self.get_nodes is not None:
            for real_node in self.get_nodes(fgraph, node):
                if real_node == "output":
                    continue
                ret = self.transform(fgraph, real_node, get_nodes=False)
                if ret is not False and ret is not None:
                    return dict(zip(real_node.outputs, ret))

        if node.op != self.op:
            return False

        s = unify(self.in_pattern, node.out)

        if s is False:
            return False

        ret = reify(self.out_pattern, s)

        if isinstance(ret, ExpressionTuple):
            ret = ret.evaled_obj

        if self.values_eq_approx:
            ret.tag.values_eq_approx = self.values_eq_approx

        if ret.owner:
            if [out.type for out in ret.owner.outputs] != [
                out.type for out in node.outputs
            ]:
                return False
        else:
            # ret is just an input variable
            assert len(node.outputs) == 1
            if ret.type != node.outputs[0].type:
                return False

        return [ret]

    def __str__(self):
        if getattr(self, "__name__", None):
            return self.__name__

        def pattern_to_str(pattern):
            if isinstance(pattern, (list, tuple)):
                return "{}({})".format(
                    str(pattern[0]),
                    ", ".join([pattern_to_str(p) for p in pattern[1:]]),
                )
            elif isinstance(pattern, dict):
                return "{} subject to {}".format(
                    pattern_to_str(pattern["pattern"]),
                    str(pattern.get("constraint", "no conditions")),
                )
            else:
                return str(pattern)

        return "{} -> {}".format(
            pattern_to_str(self.in_pattern),
            pattern_to_str(self.out_pattern),
        )

    def __repr__(self):
        return str(self)

    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        name = getattr(self, "__name__", getattr(self, "name", None))
        print(
            f"{' ' * level}{self.__class__.__name__} {name}({self.in_pattern}, {self.out_pattern}) id={id(self)}",
            file=stream,
        )


class Updater(Feature):
    def __init__(self, importer, pruner, chin, name=None):
        self.importer = importer
        self.pruner = pruner
        self.chin = chin
        self.name = name

    def __str__(self):
        return "Updater{%s}" % str(self.name)

    def on_import(self, fgraph, node, reason):
        if self.importer:
            self.importer(node)

    def on_prune(self, fgraph, node, reason):
        if self.pruner:
            self.pruner(node)

    def on_change_input(self, fgraph, node, i, r, new_r, reason):
        if self.chin:
            self.chin(node, i, r, new_r, reason)

    def on_detach(self, fgraph):
        # To allow pickling this object
        self.importer = None
        self.pruner = None
        self.chin = None


class NavigatorOptimizer(GlobalOptimizer):
    r"""An optimizer that applies a `LocalOptimizer` with considerations for the new nodes it creates.


    This optimizer also allows the `LocalOptimizer` to use a special ``"remove"`` value
    in the ``dict``\s returned by :meth:`LocalOptimizer`.  `Variable`\s mapped to this
    value are removed from the `FunctionGraph`.

    Parameters
    ----------
    local_opt :
        A `LocalOptimizer` to apply over a `FunctionGraph` (or ``None``).
    ignore_newtrees :
        - ``True``: new subgraphs returned by an optimization are not a
          candidate for optimization.
        - ``False``: new subgraphs returned by an optimization is a candidate
          for optimization.
        - ``'auto'``: let the `local_opt` set this parameter via its :attr:`reentrant`
          attribute.
    failure_callback
        A function with the signature ``(exception, navigator, [(old, new),
        (old,new),...])`` that is called when there's an exception.

        If the exception is raised in ``local_opt.transform``, the ``new`` variables
        will be ``None``.

        If the exception is raised during validation (e.g. the new types don't
        match) then the new variables will be the ones created by ``self.transform``.

        If this parameter is ``None``, then exceptions are not caught here and
        are raised normally.

    """

    @staticmethod
    def warn(exc, nav, repl_pairs, local_opt, node):
        """A failure callback that prints a traceback."""
        if config.on_opt_error != "ignore":
            _logger.error(f"Optimization failure due to: {local_opt}")
            _logger.error(f"node: {node}")
            _logger.error("TRACEBACK:")
            _logger.error(traceback.format_exc())
        if config.on_opt_error == "pdb":
            pdb.post_mortem(sys.exc_info()[2])
        elif isinstance(exc, AssertionError) or config.on_opt_error == "raise":
            # We always crash on AssertionError because something may be
            # seriously wrong if such an exception is raised.
            raise exc

    @staticmethod
    def warn_inplace(exc, nav, repl_pairs, local_opt, node):
        r"""A failure callback that ignores ``InconsistencyError``\s and prints a traceback.

        If the error occurred during replacement, ``repl_pairs`` is set;
        otherwise, its value is ``None``.

        """
        if isinstance(exc, InconsistencyError):
            return
        return NavigatorOptimizer.warn(exc, nav, repl_pairs, local_opt, node)

    @staticmethod
    def warn_ignore(exc, nav, repl_pairs, local_opt, node):
        """A failure callback that ignores all errors."""

    def __init__(self, local_opt, ignore_newtrees="auto", failure_callback=None):
        self.local_opt = local_opt
        if ignore_newtrees == "auto":
            self.ignore_newtrees = not getattr(local_opt, "reentrant", True)
        else:
            self.ignore_newtrees = ignore_newtrees
        self.failure_callback = failure_callback

    def attach_updater(self, fgraph, importer, pruner, chin=None, name=None):
        r"""Install `FunctionGraph` listeners to help the navigator deal with the ``ignore_trees``-related functionality.

        Parameters
        ----------
        importer :
            Function that will be called whenever optimizations add stuff
            to the graph.
        pruner :
            Function to be called when optimizations remove stuff
            from the graph.
        chin :
            "on change input" called whenever a node's inputs change.
        name :
            name of the ``Updater`` to attach.

        Returns
        -------
        The `FunctionGraph` plugin that handles the three tasks.
        Keep this around so that `Feature`\s can be detached later.

        """
        if self.ignore_newtrees:
            importer = None

        if importer is None and pruner is None:
            return None

        u = Updater(importer, pruner, chin, name=name)
        fgraph.attach_feature(u)
        return u

    def detach_updater(self, fgraph, u):
        """Undo the work of ``attach_updater``.

        Parameters
        ----------
        fgraph
            The `FunctionGraph`.
        u
            A return-value of ``attach_updater``.

        Returns
        -------
        None

        """
        if u is not None:
            fgraph.remove_feature(u)

    def process_node(self, fgraph, node, lopt=None):
        r"""Apply `lopt` to `node`.

        The :meth:`lopt.transform` method will return either ``False`` or a
        list of `Variable`\s that are intended to replace :attr:`node.outputs`.

        If the `fgraph` accepts the replacement, then the optimization is
        successful, and this function returns ``True``.

        If there are no replacement candidates or the `fgraph` rejects the
        replacements, this function returns ``False``.

        Parameters
        ----------
        fgraph :
            A `FunctionGraph`.
        node :
            An `Apply` instance in `fgraph`
        lopt :
            A `LocalOptimizer` instance that may have a better idea for
            how to compute node's outputs.

        Returns
        -------
        bool
            ``True`` iff the `node`'s outputs were replaced in the `fgraph`.

        """
        lopt = lopt or self.local_opt
        try:
            replacements = lopt.transform(fgraph, node)
        except Exception as e:
            if self.failure_callback is not None:
                self.failure_callback(
                    e, self, [(x, None) for x in node.outputs], lopt, node
                )
                return False
            else:
                raise
        if replacements is False or replacements is None:
            return False
        old_vars = node.outputs
        remove = []
        if isinstance(replacements, dict):
            if "remove" in replacements:
                remove = replacements.pop("remove")
            old_vars = list(replacements.keys())
            replacements = list(replacements.values())
        elif not isinstance(replacements, (tuple, list)):
            raise TypeError(
                f"Local optimizer {lopt} gave wrong type of replacement. "
                f"Expected list or tuple; got {replacements}"
            )
        if len(old_vars) != len(replacements):
            raise ValueError(
                f"Local optimizer {lopt} gave wrong number of replacements"
            )
        # None in the replacement mean that this variable isn't used
        # and we want to remove it
        for r, rnew in zip(old_vars, replacements):
            if rnew is None and len(fgraph.clients[r]) > 0:
                raise ValueError(
                    f"Local optimizer {lopt} tried to remove a variable"
                    f" that is being used: {r}"
                )
        # If an output would be replaced by itself, no need to perform
        # the replacement
        repl_pairs = [
            (r, rnew)
            for r, rnew in zip(old_vars, replacements)
            if rnew is not r and rnew is not None
        ]

        if len(repl_pairs) == 0:
            return False
        try:
            fgraph.replace_all_validate_remove(repl_pairs, reason=lopt, remove=remove)
            return True
        except Exception as e:
            # This means the replacements were rejected by the fgraph.
            #
            # This is not supposed to happen.  The default failure_callback
            # will print a traceback as a warning.
            if self.failure_callback is not None:
                self.failure_callback(e, self, repl_pairs, lopt, node)
                return False
            else:
                raise

    def add_requirements(self, fgraph):
        super().add_requirements(fgraph)
        # Added by default
        # fgraph.attach_feature(ReplaceValidate())
        if self.local_opt:
            self.local_opt.add_requirements(fgraph)

    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        print(f"{' ' * level}{self.__class__.__name__} id={id(self)}", file=stream)
        if depth != 0:
            self.local_opt.print_summary(stream, level=(level + 2), depth=(depth - 1))


class TopoOptimizer(NavigatorOptimizer):
    """An optimizer that applies a single `LocalOptimizer` to each node in topological order (or reverse)."""

    def __init__(
        self, local_opt, order="in_to_out", ignore_newtrees=False, failure_callback=None
    ):
        if order not in ("out_to_in", "in_to_out"):
            raise ValueError("order must be 'out_to_in' or 'in_to_out'")
        self.order = order
        super().__init__(local_opt, ignore_newtrees, failure_callback)

    def apply(self, fgraph, start_from=None):
        if start_from is None:
            start_from = fgraph.outputs
        callback_before = fgraph.execute_callbacks_time
        nb_nodes_start = len(fgraph.apply_nodes)
        t0 = time.time()
        q = deque(io_toposort(fgraph.inputs, start_from))
        io_t = time.time() - t0

        def importer(node):
            if node is not current_node:
                q.append(node)

        u = self.attach_updater(
            fgraph, importer, None, name=getattr(self, "name", None)
        )
        nb = 0
        try:
            t0 = time.time()
            while q:
                if self.order == "out_to_in":
                    node = q.pop()
                else:
                    node = q.popleft()
                if node not in fgraph.apply_nodes:
                    continue
                current_node = node
                nb += self.process_node(fgraph, node)
            loop_t = time.time() - t0
        finally:
            self.detach_updater(fgraph, u)

        callback_time = fgraph.execute_callbacks_time - callback_before
        nb_nodes_end = len(fgraph.apply_nodes)
        return (
            self,
            nb,
            nb_nodes_start,
            nb_nodes_end,
            io_t,
            loop_t,
            callback_time,
            self.local_opt,
        )

    @staticmethod
    def print_profile(stream, prof, level=0):
        blanc = "    " * level
        if prof is None:  # Happen as merge_profile() isn't implemented
            print(blanc, "TopoOptimizer merge_profile not implemented", file=stream)
            return

        (
            opt,
            nb,
            nb_nodes_start,
            nb_nodes_end,
            io_t,
            loop_t,
            callback_time,
            lopt,
        ) = prof

        print(
            blanc,
            "TopoOptimizer ",
            getattr(opt, "name", getattr(opt, "__name__", "")),
            file=stream,
        )

        print(
            blanc,
            "  nb_node (start, end, changed)",
            (nb_nodes_start, nb_nodes_end, nb),
            file=stream,
        )
        print(blanc, "  init io_toposort", io_t, file=stream)
        print(blanc, "  loop time", loop_t, file=stream)
        print(blanc, "  callback_time", callback_time, file=stream)
        if isinstance(lopt, LocalOptGroup):
            if lopt.profile:
                lopt.print_profile(
                    stream,
                    (
                        lopt.time_opts,
                        lopt.process_count,
                        lopt.applied_true,
                        lopt.node_created,
                        lopt.profile,
                    ),
                    level=level + 1,
                )

    def __str__(self):
        return getattr(self, "__name__", "<TopoOptimizer instance>")


def topogroup_optimizer(order, *local_opts, name=None, **kwargs):
    """Apply `local_opts` from the input/output nodes to the output/input nodes of a graph.

    This uses a combination of `LocalOptGroup` and `TopoOptimizer`.
    """
    if len(local_opts) > 1:
        # Don't wrap it uselessly if their is only 1 optimization.
        local_opts = LocalOptGroup(*local_opts)
    else:
        (local_opts,) = local_opts
        if not name:
            name = local_opts.__name__
    ret = TopoOptimizer(
        local_opts,
        order="in_to_out",
        failure_callback=TopoOptimizer.warn_inplace,
        **kwargs,
    )
    if name:
        ret.__name__ = name
    return ret


in2out = partial(topogroup_optimizer, "in_to_out")
out2in = partial(topogroup_optimizer, "out_to_in")


class OpKeyOptimizer(NavigatorOptimizer):
    r"""An optimizer that applies a `LocalOptimizer` to specific `Op`\s.

    The `Op`\s are provided by a :meth:`LocalOptimizer.op_key` method (either
    as a list of `Op`\s or a single `Op`), and discovered within a
    `FunctionGraph` using the `NodeFinder` `Feature`.

    This is similar to the ``tracks`` feature used by other optimizers.

    """

    def __init__(self, local_opt, ignore_newtrees=False, failure_callback=None):
        if not hasattr(local_opt, "op_key"):
            raise TypeError(f"{local_opt} must have an `op_key` method.")
        super().__init__(local_opt, ignore_newtrees, failure_callback)

    def apply(self, fgraph):
        op = self.local_opt.op_key()
        if isinstance(op, (list, tuple)):
            q = reduce(list.__iadd__, map(fgraph.get_nodes, op))
        else:
            q = list(fgraph.get_nodes(op))

        def importer(node):
            if node is not current_node:
                if node.op == op:
                    q.append(node)

        u = self.attach_updater(
            fgraph, importer, None, name=getattr(self, "name", None)
        )
        try:
            while q:
                node = q.pop()
                if node not in fgraph.apply_nodes:
                    continue
                current_node = node
                self.process_node(fgraph, node)
        finally:
            self.detach_updater(fgraph, u)

    def add_requirements(self, fgraph):
        super().add_requirements(fgraph)
        fgraph.attach_feature(NodeFinder())


class ChangeTracker(Feature):
    def __init__(self):
        self.changed = False
        self.nb_imported = 0

    def on_import(self, fgraph, node, reason):
        self.nb_imported += 1
        self.changed = True

    def on_change_input(self, fgraph, node, i, r, new_r, reason):
        self.changed = True

    def reset(self):
        self.changed = False

    def on_attach(self, fgraph):
        fgraph.change_tracker = self

    def on_detach(self, fgraph):
        del fgraph.change_tracker


def merge_dict(d1, d2):
    r"""Merge two ``dict``\s by adding their values."""
    d = d1.copy()
    for k, v in d2.items():
        if k in d:
            d[k] += v
        else:
            d[k] = v
    return d


class EquilibriumOptimizer(NavigatorOptimizer):
    """An optimizer that applies an optimization until a fixed-point/equilibrium is reached.

    Parameters
    ----------
    optimizers : list or set
        Local or global optimizations to apply until equilibrium.
        The global optimizer will be run at the start of each iteration before
        the local optimizer.
    max_use_ratio : int or float
        Each optimizer can be applied at most ``(size of graph * this number)``
        times.
    ignore_newtrees :
        See :attr:`EquilibriumDB.ignore_newtrees`.
    final_optimizers :
        Global optimizers that will be run after each iteration.
    cleanup_optimizers :
        Global optimizers that apply a list of pre determined optimization.
        They must not traverse the graph as they are called very frequently.
        The MergeOptimizer is one example of optimization that respect this.
        They are applied after all global optimizers, then when one local
        optimizer is applied, then after all final optimizers.

    """

    def __init__(
        self,
        optimizers,
        failure_callback=None,
        ignore_newtrees=True,
        tracks_on_change_inputs=False,
        max_use_ratio=None,
        final_optimizers=None,
        cleanup_optimizers=None,
    ):
        super().__init__(
            None, ignore_newtrees=ignore_newtrees, failure_callback=failure_callback
        )
        self.global_optimizers = []
        self.final_optimizers = []
        self.cleanup_optimizers = []
        self.tracks_on_change_inputs = tracks_on_change_inputs

        self.local_tracker = LocalOptTracker()

        for opt in optimizers:
            if isinstance(opt, LocalOptimizer):
                self.local_tracker.add_tracker(opt)
            else:
                self.global_optimizers.append(opt)

        if final_optimizers:
            self.final_optimizers = final_optimizers
        if cleanup_optimizers:
            self.cleanup_optimizers = cleanup_optimizers
        self.max_use_ratio = max_use_ratio

    def get_local_optimizers(self):
        yield from self.local_tracker.get_rewriters()

    def add_requirements(self, fgraph):
        super().add_requirements(fgraph)
        for opt in self.get_local_optimizers():
            opt.add_requirements(fgraph)
        for opt in self.global_optimizers:
            opt.add_requirements(fgraph)
        for opt in self.final_optimizers:
            opt.add_requirements(fgraph)
        for opt in self.cleanup_optimizers:
            opt.add_requirements(fgraph)

    def apply(self, fgraph, start_from=None):
        change_tracker = ChangeTracker()
        fgraph.attach_feature(change_tracker)
        if start_from is None:
            start_from = fgraph.outputs
        else:
            for node in start_from:
                assert node in fgraph.outputs

        changed = True
        max_use_abort = False
        opt_name = None
        global_process_count = {}
        start_nb_nodes = len(fgraph.apply_nodes)
        max_nb_nodes = len(fgraph.apply_nodes)
        max_use = max_nb_nodes * self.max_use_ratio

        loop_timing = []
        loop_process_count = []
        global_opt_timing = []
        time_opts = {}
        io_toposort_timing = []
        nb_nodes = []
        node_created = {}
        global_sub_profs = []
        final_sub_profs = []
        cleanup_sub_profs = []
        for opt in (
            self.global_optimizers
            + list(self.get_local_optimizers())
            + self.final_optimizers
            + self.cleanup_optimizers
        ):
            global_process_count.setdefault(opt, 0)
            time_opts.setdefault(opt, 0)
            node_created.setdefault(opt, 0)

        def apply_cleanup(profs_dict):
            changed = False
            for copt in self.cleanup_optimizers:
                change_tracker.reset()
                nb = change_tracker.nb_imported
                t_opt = time.time()
                sub_prof = copt.apply(fgraph)
                time_opts[copt] += time.time() - t_opt
                profs_dict[copt].append(sub_prof)
                if change_tracker.changed:
                    process_count.setdefault(copt, 0)
                    process_count[copt] += 1
                    global_process_count[copt] += 1
                    changed = True
                    node_created[copt] += change_tracker.nb_imported - nb
            return changed

        while changed and not max_use_abort:
            process_count = {}
            t0 = time.time()
            changed = False
            iter_cleanup_sub_profs = {}
            for copt in self.cleanup_optimizers:
                iter_cleanup_sub_profs[copt] = []

            # apply global optimizers
            sub_profs = []
            for gopt in self.global_optimizers:
                change_tracker.reset()
                nb = change_tracker.nb_imported
                t_opt = time.time()
                sub_prof = gopt.apply(fgraph)
                time_opts[gopt] += time.time() - t_opt
                sub_profs.append(sub_prof)
                if change_tracker.changed:
                    process_count.setdefault(gopt, 0)
                    process_count[gopt] += 1
                    global_process_count[gopt] += 1
                    changed = True
                    node_created[gopt] += change_tracker.nb_imported - nb
                    if global_process_count[gopt] > max_use:
                        max_use_abort = True
                        opt_name = getattr(gopt, "name", None) or getattr(
                            gopt, "__name__", ""
                        )
            global_sub_profs.append(sub_profs)

            global_opt_timing.append(float(time.time() - t0))

            # apply clean up as global opt can have done changes that
            # request that
            changed |= apply_cleanup(iter_cleanup_sub_profs)

            # apply local optimizer
            topo_t0 = time.time()
            q = deque(io_toposort(fgraph.inputs, start_from))
            io_toposort_timing.append(time.time() - topo_t0)

            nb_nodes.append(len(q))
            max_nb_nodes = max(max_nb_nodes, len(q))
            max_use = max_nb_nodes * self.max_use_ratio

            def importer(node):
                if node is not current_node:
                    q.append(node)

            chin = None
            if self.tracks_on_change_inputs:

                def chin(node, i, r, new_r, reason):
                    if node is not current_node and not isinstance(node, str):
                        q.append(node)

            u = self.attach_updater(
                fgraph, importer, None, chin=chin, name=getattr(self, "name", None)
            )
            try:
                while q:
                    node = q.pop()
                    if node not in fgraph.apply_nodes:
                        continue
                    current_node = node
                    for lopt in self.local_tracker.get_trackers(node.op):
                        nb = change_tracker.nb_imported
                        t_opt = time.time()
                        lopt_change = self.process_node(fgraph, node, lopt)
                        time_opts[lopt] += time.time() - t_opt
                        if not lopt_change:
                            continue
                        process_count.setdefault(lopt, 0)
                        process_count[lopt] += 1
                        global_process_count[lopt] += 1
                        changed = True
                        node_created[lopt] += change_tracker.nb_imported - nb
                        changed |= apply_cleanup(iter_cleanup_sub_profs)
                        if global_process_count[lopt] > max_use:
                            max_use_abort = True
                            opt_name = getattr(lopt, "name", None) or getattr(
                                lopt, "__name__", ""
                            )
                        if node not in fgraph.apply_nodes:
                            # go to next node
                            break
            finally:
                self.detach_updater(fgraph, u)

            # Apply final optimizers
            sub_profs = []
            t_before_final_opt = time.time()
            for gopt in self.final_optimizers:
                change_tracker.reset()
                nb = change_tracker.nb_imported
                t_opt = time.time()
                sub_prof = gopt.apply(fgraph)
                time_opts[gopt] += time.time() - t_opt
                sub_profs.append(sub_prof)
                if change_tracker.changed:
                    process_count.setdefault(gopt, 0)
                    process_count[gopt] += 1
                    global_process_count[gopt] += 1
                    changed = True
                    node_created[gopt] += change_tracker.nb_imported - nb
                    if global_process_count[gopt] > max_use:
                        max_use_abort = True
                        opt_name = getattr(gopt, "name", None) or getattr(
                            gopt, "__name__", ""
                        )
            final_sub_profs.append(sub_profs)

            global_opt_timing[-1] += time.time() - t_before_final_opt
            # apply clean up as final opt can have done changes that
            # request that
            changed |= apply_cleanup(iter_cleanup_sub_profs)
            # merge clean up profiles during that iteration.
            c_sub_profs = []
            for copt, sub_profs in iter_cleanup_sub_profs.items():
                sub_prof = sub_profs[0]
                for s_p in sub_profs[1:]:
                    sub_prof = copt.merge_profile(sub_prof, s_p)
                c_sub_profs.append(sub_prof)
            cleanup_sub_profs.append(c_sub_profs)

            loop_process_count.append(process_count)
            loop_timing.append(float(time.time() - t0))

        end_nb_nodes = len(fgraph.apply_nodes)

        if max_use_abort:
            msg = (
                f"EquilibriumOptimizer max'ed out by '{opt_name}'"
                + ". You can safely raise the current threshold of "
                + "{config.optdb__max_use_ratio:f} with the aesara flag 'optdb__max_use_ratio'."
            )
            if config.on_opt_error == "raise":
                raise AssertionError(msg)
            else:
                _logger.error(msg)
        fgraph.remove_feature(change_tracker)
        assert len(loop_process_count) == len(loop_timing)
        assert len(loop_process_count) == len(global_opt_timing)
        assert len(loop_process_count) == len(nb_nodes)
        assert len(loop_process_count) == len(io_toposort_timing)
        assert len(loop_process_count) == len(global_sub_profs)
        assert len(loop_process_count) == len(final_sub_profs)
        assert len(loop_process_count) == len(cleanup_sub_profs)
        return (
            self,
            loop_timing,
            loop_process_count,
            (start_nb_nodes, end_nb_nodes, max_nb_nodes),
            global_opt_timing,
            nb_nodes,
            time_opts,
            io_toposort_timing,
            node_created,
            global_sub_profs,
            final_sub_profs,
            cleanup_sub_profs,
        )

    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        name = getattr(self, "name", None)
        print(
            f"{' ' * level}{self.__class__.__name__} {name} id={id(self)}", file=stream
        )
        if depth != 0:
            for lopt in self.get_local_optimizers():
                lopt.print_summary(stream, level=(level + 2), depth=(depth - 1))

    @staticmethod
    def print_profile(stream, prof, level=0):
        (
            opt,
            loop_timing,
            loop_process_count,
            (start_nb_nodes, end_nb_nodes, max_nb_nodes),
            global_opt_timing,
            nb_nodes,
            time_opts,
            io_toposort_timing,
            node_created,
            global_sub_profs,
            final_sub_profs,
            cleanup_sub_profs,
        ) = prof

        blanc = "    " * level
        print(blanc, "EquilibriumOptimizer", end=" ", file=stream)
        print(blanc, getattr(opt, "name", getattr(opt, "__name__", "")), file=stream)
        print(
            blanc,
            f"  time {sum(loop_timing):.3f}s for {len(loop_timing)} passes",
            file=stream,
        )
        print(
            blanc,
            f"  nb nodes (start, end,  max) {int(start_nb_nodes)} {int(end_nb_nodes)} {int(max_nb_nodes)}",
            file=stream,
        )
        print(blanc, f"  time io_toposort {sum(io_toposort_timing):.3f}s", file=stream)
        s = sum([time_opts[o] for o in opt.get_local_optimizers()])
        print(blanc, f"  time in local optimizers {s:.3f}s", file=stream)
        s = sum([time_opts[o] for o in opt.global_optimizers])
        print(blanc, f"  time in global optimizers {s:.3f}s", file=stream)
        s = sum([time_opts[o] for o in opt.final_optimizers])
        print(blanc, f"  time in final optimizers {s:.3f}s", file=stream)
        s = sum([time_opts[o] for o in opt.cleanup_optimizers])
        print(blanc, f"  time in cleanup optimizers {s:.3f}s", file=stream)
        for i in range(len(loop_timing)):
            lopt = ""
            if loop_process_count[i]:
                d = list(
                    reversed(sorted(loop_process_count[i].items(), key=lambda a: a[1]))
                )
                lopt = " ".join([str((str(k), v)) for k, v in d[:5]])
                if len(d) > 5:
                    lopt += " ..."
            print(
                blanc,
                (
                    f"  {int(i):2d} - {loop_timing[i]:.3f}s {int(sum(loop_process_count[i].values()))} ({global_opt_timing[i]:.3f}s in global opts, "
                    f"{io_toposort_timing[i]:.3f}s io_toposort) - {int(nb_nodes[i])} nodes - {lopt}"
                ),
                file=stream,
            )

        count_opt = []
        not_used = []
        not_used_time = 0
        process_count = {}
        for o in (
            opt.global_optimizers
            + list(opt.get_local_optimizers())
            + list(opt.final_optimizers)
            + list(opt.cleanup_optimizers)
        ):
            process_count.setdefault(o, 0)
        for count in loop_process_count:
            for o, v in count.items():
                process_count[o] += v
        for o, count in process_count.items():
            if count > 0:
                count_opt.append((time_opts[o], count, node_created[o], o))
            else:
                not_used.append((time_opts[o], o))
                not_used_time += time_opts[o]

        if count_opt:
            print(
                blanc, "  times - times applied - nb node created - name:", file=stream
            )
            count_opt.sort()
            for (t, count, n_created, o) in count_opt[::-1]:
                print(
                    blanc,
                    f"  {t:.3f}s - {int(count)} - {int(n_created)} - {o}",
                    file=stream,
                )
            print(
                blanc,
                f"  {not_used_time:.3f}s - in {len(not_used)} optimization that were not used (display only those with a runtime > 0)",
                file=stream,
            )
            not_used.sort(key=lambda nu: (nu[0], str(nu[1])))
            for (t, o) in not_used[::-1]:
                if t > 0:
                    # Skip opt that have 0 times, they probably wasn't even tried.
                    print(blanc + "  ", f"  {t:.3f}s - {o}", file=stream)
            print(file=stream)
        gf_opts = [
            o
            for o in (
                opt.global_optimizers
                + list(opt.final_optimizers)
                + list(opt.cleanup_optimizers)
            )
            if o.print_profile.__code__ is not GlobalOptimizer.print_profile.__code__
        ]
        if not gf_opts:
            return
        print(blanc, "Global, final and clean up optimizers", file=stream)
        for i in range(len(loop_timing)):
            print(blanc, f"Iter {int(i)}", file=stream)
            for o, prof in zip(opt.global_optimizers, global_sub_profs[i]):
                try:
                    o.print_profile(stream, prof, level + 2)
                except NotImplementedError:
                    print(blanc, "merge not implemented for ", o)
            for o, prof in zip(opt.final_optimizers, final_sub_profs[i]):
                try:
                    o.print_profile(stream, prof, level + 2)
                except NotImplementedError:
                    print(blanc, "merge not implemented for ", o)
            for o, prof in zip(opt.cleanup_optimizers, cleanup_sub_profs[i]):
                try:
                    o.print_profile(stream, prof, level + 2)
                except NotImplementedError:
                    print(blanc, "merge not implemented for ", o)

    @staticmethod
    def merge_profile(prof1, prof2):
        # (opt, loop_timing, loop_process_count, max_nb_nodes,
        # global_opt_timing, nb_nodes, time_opts, io_toposort_timing) = prof1
        local_optimizers = OrderedSet(prof1[0].get_local_optimizers()).union(
            prof2[0].get_local_optimizers()
        )
        global_optimizers = OrderedSet(prof1[0].global_optimizers).union(
            prof2[0].global_optimizers
        )
        final_optimizers = list(
            OrderedSet(prof1[0].final_optimizers).union(prof2[0].final_optimizers)
        )
        cleanup_optimizers = list(
            OrderedSet(prof1[0].cleanup_optimizers).union(prof2[0].cleanup_optimizers)
        )
        new_opt = EquilibriumOptimizer(
            local_optimizers.union(global_optimizers),
            max_use_ratio=1,
            final_optimizers=final_optimizers,
            cleanup_optimizers=cleanup_optimizers,
        )

        def add_append_list(l1, l2):
            l = copy.copy(l1)
            for idx, nb in enumerate(l2):
                if idx < len(l):
                    l[idx] += nb
                else:
                    l.append(nb)
            return l

        loop_timing = add_append_list(prof1[1], prof2[1])

        loop_process_count = list(prof1[2])
        global_sub_profs = []
        final_sub_profs = []
        cleanup_sub_profs = []

        for i in range(min(len(loop_process_count), len(prof2[2]))):
            process_count = loop_process_count[i]
            for process, count in prof2[2][i].items():
                if process in process_count:
                    process_count[process] += count
                else:
                    process_count[process] = count

            def merge(opts, attr, idx):
                tmp = []
                for opt in opts:
                    o1 = getattr(prof1[0], attr)
                    o2 = getattr(prof2[0], attr)
                    if opt in o1 and opt in o2:
                        p1 = prof1[idx][i][o1.index(opt)]
                        p2 = prof2[idx][i][o2.index(opt)]
                        m = None
                        if hasattr(opt, "merge_profile"):
                            m = opt.merge_profile(p1, p2)
                    elif opt in o1:
                        m = prof1[idx][i][o1.index(opt)]
                    else:
                        m = prof2[idx][i][o2.index(opt)]
                    tmp.append(m)
                return tmp

            global_sub_profs.append(merge(global_optimizers, "global_optimizers", 9))
            final_sub_profs.append(merge(final_optimizers, "final_optimizers", 10))
            cleanup_sub_profs.append(
                merge(cleanup_optimizers, "cleanup_optimizers", 11)
            )

        # Add the iteration done by only one of the profile.
        loop_process_count.extend(prof1[2][len(loop_process_count) :])
        global_sub_profs.extend(prof1[9][len(global_sub_profs) :])
        final_sub_profs.extend(prof1[10][len(final_sub_profs) :])
        cleanup_sub_profs.extend(prof1[11][len(cleanup_sub_profs) :])

        global_sub_profs.extend(prof2[9][len(loop_process_count) :])
        final_sub_profs.extend(prof2[10][len(loop_process_count) :])
        cleanup_sub_profs.extend(prof2[11][len(loop_process_count) :])

        max_nb_nodes = max(prof1[3], prof2[3])

        global_opt_timing = add_append_list(prof1[4], prof2[4])

        nb_nodes = add_append_list(prof1[5], prof2[5])

        time_opts = merge_dict(prof1[6], prof2[6])
        io_toposort_timing = add_append_list(prof1[7], prof2[7])
        assert (
            len(loop_timing)
            == len(global_opt_timing)
            == len(global_sub_profs)
            == len(io_toposort_timing)
            == len(nb_nodes)
        )
        assert len(loop_timing) == max(len(prof1[1]), len(prof2[1]))

        node_created = merge_dict(prof1[8], prof2[8])
        return (
            new_opt,
            loop_timing,
            loop_process_count,
            max_nb_nodes,
            global_opt_timing,
            nb_nodes,
            time_opts,
            io_toposort_timing,
            node_created,
            global_sub_profs,
            final_sub_profs,
            cleanup_sub_profs,
        )


def _check_chain(r, chain):
    """
    WRITEME

    """
    chain = list(reversed(chain))
    while chain:
        elem = chain.pop()
        if elem is None:
            if r.owner is not None:
                return False
        elif r.owner is None:
            return False
        elif isinstance(elem, Op):
            if not r.owner.op == elem:
                return False
        else:
            try:
                if issubclass(elem, Op) and not isinstance(r.owner.op, elem):
                    return False
            except TypeError:
                return False
        if chain:
            r = r.owner.inputs[chain.pop()]
    # print 'check_chain', _check_chain.n_calls
    # _check_chain.n_calls += 1

    # The return value will be used as a Boolean, but some Variables cannot
    # be used as Booleans (the results of comparisons, for instance)
    return r is not None


def check_chain(r, *chain):
    """
    WRITEME

    """
    if isinstance(r, Apply):
        r = r.outputs[0]
    return _check_chain(r, reduce(list.__iadd__, ([x, 0] for x in chain)))


def pre_greedy_local_optimizer(fgraph, optimizations, out):
    """Apply local optimizations to a graph.

    This function traverses the computation graph in the graph before the
    variable `out` but that are not in the `fgraph`. It applies
    `optimizations` to each variable on the traversed graph.

    .. warning::

        This changes the nodes in a graph in-place.

    Its main use is to apply locally constant folding when generating
    the graph of the indices of a subtensor.

    Changes should not be applied to nodes that are in an `fgraph`,
    so we use `fgraph` to prevent that.

    Notes
    -----
    This doesn't do an equilibrium optimization, so, if there is an
    optimization--like `local_upcast_elemwise_constant_inputs`--in the list
    that adds additional nodes to the inputs of the node, it might be necessary
    to call this function multiple times.

    Parameters
    ----------
    fgraph : FunctionGraph
        The graph used to avoid/filter nodes.
    optimizations : list of LocalOptimizer
        The list of local optimizations to apply
    out : Variable
        A `Variable` specifying the graph to optimize.

    """

    def local_recursive_function(list_opt, out, optimized_vars, depth):
        if not getattr(out, "owner", None):
            return [out], optimized_vars
        node = out.owner

        if node in fgraph.apply_nodes:
            return node.outputs, optimized_vars

        # Walk up the graph via the node's inputs
        for idx, inp in enumerate(node.inputs):
            if inp in optimized_vars:
                nw_in = optimized_vars[inp]
            else:
                if inp.owner:
                    outs, optimized_vars = local_recursive_function(
                        list_opt, inp, optimized_vars, depth + 1
                    )
                    for k, v in zip(inp.owner.outputs, outs):
                        optimized_vars[k] = v
                    nw_in = outs[inp.owner.outputs.index(inp)]

                else:
                    nw_in = inp
                    optimized_vars[inp] = inp

            # XXX: An in-place change
            node.inputs[idx] = nw_in

        # Apply the optimizations
        results = node.outputs
        for opt in list_opt:
            ret = opt.transform(fgraph, node)
            if ret is not False and ret is not None:
                assert len(ret) == len(node.outputs), opt
                for k, v in zip(node.outputs, ret):
                    optimized_vars[k] = v
                results = ret
                if ret[0].owner:
                    node = out.owner
                else:
                    break

        return results, optimized_vars

    if out.owner:
        out_index = out.owner.outputs.index(out)
    else:
        out_index = 0

    final_outs, optimized_nodes = local_recursive_function(optimizations, out, {}, 0)
    return final_outs[out_index]


def copy_stack_trace(from_var, to_var):
    r"""Copy the stack traces from `from_var` to `to_var`.

    Parameters
    ----------
    from_var :
        `Variable` or list `Variable`\s to copy stack traces from.
    to_var :
        `Variable` or list `Variable`\s to copy stack traces to.

    Notes
    -----
    The stacktrace is assumed to be of the form of a list of lists
    of tuples. Each tuple contains the filename, line number, function name
    and so on. Each list of tuples contains the truples belonging to a
    particular `Variable`.

    """

    # Store stack traces from from_var
    tr = []
    if isinstance(from_var, Iterable) and not isinstance(from_var, Variable):
        # If from_var is a list, store concatenated stack traces
        for v in from_var:
            tr += getattr(v.tag, "trace", [])

    else:
        # If from_var is not a list, it must be a single tensor variable,
        # so just store that particular stack trace
        tr = getattr(from_var.tag, "trace", [])

    if tr and isinstance(tr[0], tuple):
        # There was one single stack trace, we encapsulate it in a list
        tr = [tr]

    # Copy over stack traces to to_var
    if isinstance(to_var, Iterable) and not isinstance(to_var, Variable):
        # Copy over stack traces from from_var to each variable in
        # to_var, including the stack_trace of the to_var before
        for v in to_var:
            v.tag.trace = getattr(v.tag, "trace", []) + tr
    else:
        # Copy over stack traces from from_var to each variable to
        # to_var, including the stack_trace of the to_var before
        to_var.tag.trace = getattr(to_var.tag, "trace", []) + tr
    return to_var


@contextlib.contextmanager
def inherit_stack_trace(from_var):
    """
    A context manager that copies the stack trace from one or more variable nodes to all
    variable nodes constructed in the body. ``new_nodes`` is the list of all the newly created
    variable nodes inside an optimization that is managed by ``graph.nodes_constructed``.

    Parameters
    ----------
    from_var :
        `Variable` node or a list of `Variable` nodes to copy stack traces from.

    """
    with nodes_constructed() as new_nodes:
        yield
    copy_stack_trace(from_var, new_nodes)


def check_stack_trace(f_or_fgraph, ops_to_check="last", bug_print="raise"):
    r"""Checks if the outputs of specific `Op`\s have a stack trace.

    Parameters
    ----------
    f_or_fgraph : Function or FunctionGraph
        The compiled function or the function graph to be analysed.
    ops_to_check
        This value can be of four different types:
            - classes or instances inheriting from `Op`
            - tuple/list of classes or instances inheriting from `Op`
            - string
            - function returning a boolean and taking as input an instance of `Op`

        - if `ops_to_check` is a string, it should be either ``'last'`` or ``'all'``.
          ``'last'`` will check only the last `Op` of the graph while ``'all'`` will
          check all the `Op`\s of the graph.
        - if `ops_to_check` is an `Op` or a tuple/list of `Op`\s, the function will
          check that all the outputs of their occurrences in the graph have a
          stack trace.
        - if `ops_to_check` is a function, it should take as input a
          `Op` and return a boolean indicating if the input `Op` should
          be checked or not.

    bug_print
        This value is a string belonging to ``{'raise', 'warn', 'ignore'}``.
        You can specify the behaviour of the function when the specified
        `ops_to_check` are not in the graph of `f_or_fgraph`: it can either raise
        an exception, write a warning or simply ignore it.

    Returns
    -------
    boolean
        ``True`` if the outputs of the specified ops have a stack, ``False``
        otherwise.

    """
    if isinstance(f_or_fgraph, aesara.compile.function.types.Function):
        fgraph = f_or_fgraph.maker.fgraph
    elif isinstance(f_or_fgraph, aesara.graph.fg.FunctionGraph):
        fgraph = f_or_fgraph
    else:
        raise ValueError("The type of f_or_fgraph is not supported")

    if isinstance(ops_to_check, Op) or (
        inspect.isclass(ops_to_check) and issubclass(ops_to_check, Op)
    ):
        ops_to_check = (ops_to_check,)

    # if ops_to_check is a string
    if isinstance(ops_to_check, str):
        if ops_to_check == "last":
            apply_nodes_to_check = [
                fgraph.outputs[i].owner for i in range(len(fgraph.outputs))
            ]
        elif ops_to_check == "all":
            apply_nodes_to_check = fgraph.apply_nodes
        else:
            raise ValueError("The string ops_to_check is not recognised")

    # if ops_to_check is a list/tuple of ops
    elif isinstance(ops_to_check, (tuple, list)):
        # Separate classes from instances in ops_to_check
        op_instances = []
        op_classes = []
        for obj in ops_to_check:
            if isinstance(obj, Op):
                op_instances.append(obj)
            else:
                op_classes.append(obj)
        op_classes = tuple(op_classes)

        apply_nodes_to_check = [
            node for node in fgraph.apply_nodes if node.op in ops_to_check
        ] + [
            node
            for node in fgraph.apply_nodes
            if isinstance(node.op, op_classes)
            or (
                hasattr(node.op, "scalar_op")
                and isinstance(node.op.scalar_op, op_classes)
            )
        ]

    # if ops_to_check is a function
    elif callable(ops_to_check):
        apply_nodes_to_check = [
            node for node in fgraph.apply_nodes if ops_to_check(node)
        ]

    else:
        raise ValueError("ops_to_check does not have the right type")

    if not apply_nodes_to_check:
        msg = (
            "Provided op instances/classes are not in the graph or the "
            "graph is empty"
        )
        if bug_print == "warn":
            warnings.warn(msg)
        elif bug_print == "raise":
            raise Exception(msg)
        elif bug_print == "ignore":
            pass
        else:
            raise ValueError("The string bug_print is not recognised")

    for node in apply_nodes_to_check:
        for output in node.outputs:
            if not hasattr(output.tag, "trace") or not output.tag.trace:
                return False

    return True


class CheckStackTraceFeature(Feature):
    def on_import(self, fgraph, node, reason):
        # In optdb we only register the CheckStackTraceOptimization when
        # config.check_stack_trace is not off but we also double check here.
        if config.check_stack_trace != "off" and not check_stack_trace(fgraph, "all"):
            if config.check_stack_trace == "raise":
                raise AssertionError(
                    "Empty stack trace! The optimization that inserted this variable is "
                    + str(reason)
                )
            elif config.check_stack_trace in ("log", "warn"):
                apply_nodes_to_check = fgraph.apply_nodes
                for node in apply_nodes_to_check:
                    for output in node.outputs:
                        if not hasattr(output.tag, "trace") or not output.tag.trace:
                            output.tag.trace = [
                                [
                                    (
                                        "",
                                        0,
                                        "Empty stack trace! The optimization that"
                                        + "inserted this variable is "
                                        + str(reason),
                                        "",
                                    )
                                ]
                            ]
                if config.check_stack_trace == "warn":
                    warnings.warn(
                        "Empty stack trace! The optimization that inserted this variable is"
                        + str(reason)
                    )


class CheckStackTraceOptimization(GlobalOptimizer):
    """Optimizer that serves to add `CheckStackTraceOptimization` as a feature."""

    def add_requirements(self, fgraph):
        if not hasattr(fgraph, "CheckStackTraceFeature"):
            fgraph.attach_feature(CheckStackTraceFeature())

    def apply(self, fgraph):
        pass
