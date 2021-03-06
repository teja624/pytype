"""Points-to / dataflow / cfg graph engine.

It can be used to run reaching-definition queries on a nested CFG graph
and to model path-specific visibility of nested data structures.
"""


import collections
import logging


from pytype import metrics
import pytype.utils


log = logging.getLogger(__name__)


_variable_size_metric = metrics.Distribution("variable_size")


# Across a sample of 19352 modules, for files which took more than 25 seconds,
# the largest variable was, on average, 157. For files below 25 seconds, it was
# 7. Additionally, for 99% of files, the largest variable was below 64, so we
# use that as the cutoff.
MAX_VAR_SIZE = 64


class Program(object):
  """Program instances describe program entities.

  This class ties together the CFG, the data flow graph (variables + bindings)
  as well as methods. We use this for issuing IDs: We need every CFG node to
  have a unique ID, and this class does the corresponding counting.

  Attributes:
    entrypoint: Entrypoint of the program, if it has one. (None otherwise)
    cfg_nodes: CFG nodes in use. Will be used for assigning node IDs.
    variables: Variables in use. Will be used for assigning variable IDs.
  """

  def __init__(self):
    """Initialize a new (initially empty) program."""
    self.entrypoint = None
    self.cfg_nodes = []
    self.next_variable_id = 0
    self.solver = None
    self.default_data = None

  def CreateSolver(self):
    if self.solver is None:
      self.solver = Solver(self)

  def InvalidateSolver(self):
    self.solver = None

  def NewCFGNode(self, name=None, condition=None):
    """Start a new CFG node."""
    self.InvalidateSolver()
    cfg_node = CFGNode(self, name, len(self.cfg_nodes), condition)
    self.cfg_nodes.append(cfg_node)
    return cfg_node

  @property
  def variables(self):
    return {b.variable for node in self.cfg_nodes for b in node.bindings}

  def NewVariable(self, bindings=None, source_set=None, where=None):
    """Create a new Variable.

    A Variable typically models a "union type", i.e., a disjunction of different
    possible types.  This constructor assumes that all the bindings in this
    Variable have the same origin(s). If that's not the case, construct a
    variable with bindings=[] and origins=[] and then call AddBinding() to add
    the different bindings.

    Arguments:
      bindings: Optionally, a sequence of possible data items this variable can
        have.
      source_set: If we have bindings, the source_set they *all* depend on. An
        instance of SourceSet.
      where: If we have bindings, where in the CFG they're assigned.

    Returns:
      A Variable instance.
    """
    variable = Variable(self, self.next_variable_id)
    log.trace("New variable v%d" % self.next_variable_id)
    self.next_variable_id += 1
    if bindings is not None:
      assert source_set is not None and where is not None
      for data in bindings:
        binding = variable.AddBinding(data)
        binding.AddOrigin(where, source_set)
    return variable

  def MergeVariables(self, node, variables):
    """Create a combined Variable for a list of variables.

    The purpose of this function is to create a final result variable for
    functions that return a list of "temporary" variables. (E.g. function
    calls).

    Args:
      node: The current CFG node.
      variables: List of variables.
    Returns:
      A typegraph.Variable.
    """
    if not variables:
      return self.NewVariable()  # return empty var
    elif len(variables) == 1:
      v, = variables
      return v
    elif all(v is variables[0] for v in variables):
      return variables[0]
    else:
      v = self.NewVariable()
      for r in variables:
        v.PasteVariable(r, node)
      return v


class CFGNode(object):
  """A node in the CFG.

  Assignments within one CFG node are treated as unordered: E.g. if "x = x + 1"
  is in a single CFG node, both bindings for x will be visible from inside that
  node.

  Attributes:
    program: The Program instance we belong to
    id: Numerical node id.
    name: Name of this CFGNode, or None. For debugging.
    incoming: Other CFGNodes that are connected to this node.
    outgoing: CFGNodes we connect to.
    bindings: Bindings that are being assigned to Variables at this CFGNode.
    reachable_subset: A subset of the nodes reachable (going backwards) from
      this one.
    condition: None if no condition is set at this node;
               The binding representing the condition which needs to be
                 fulfilled to take the branch represented by this node.
  """
  __slots__ = ("program", "id", "name", "incoming", "outgoing", "bindings",
               "reachable_subset", "condition")

  def __init__(self, program, name, cfgnode_id, condition):
    """Initialize a new CFG node. Called from Program.NewCFGNode."""
    self.program = program
    self.id = cfgnode_id
    self.name = name
    self.incoming = set()
    self.outgoing = set()
    self.bindings = set()  # filled through RegisterBinding()
    self.reachable_subset = {self}
    self.condition = condition

  def ConnectNew(self, name=None, condition=None):
    """Add a new node connected to this node."""
    cfg_node = self.program.NewCFGNode(name, condition)
    self.ConnectTo(cfg_node)
    return cfg_node

  def ConnectTo(self, cfg_node):
    """Connect this node to an existing node."""
    self.program.InvalidateSolver()
    self.outgoing.add(cfg_node)
    cfg_node.incoming.add(self)
    cfg_node.reachable_subset |= self.reachable_subset

  def CanHaveCombination(self, bindings):
    """Quick version of HasCombination below."""
    goals = set(bindings)
    seen = set()
    stack = [self]
    # TODO(kramm): Take blocked nodes into account, like in Bindings()?
    while stack and goals:
      node = stack.pop()
      if node in seen:
        continue
      seen.add(node)
      goals -= goals & node.bindings
      stack.extend(node.incoming)
    return not goals

  def HasCombination(self, bindings):
    """Query whether a combination is possible.

    Query whether its possible to have the given combination of bindings at
    this CFG node (I.e., whether they can all be assigned at the same time.)
    This will e.g. tell us whether a return binding is possible given a specific
    combination of argument bindings.

    Arguments:
      bindings: A list of Bindings.
    Returns:
      True if the combination is possible, False otherwise.
    """
    self.program.CreateSolver()
    # Optimization: check the entire combination only if all of the bindings
    # are possible separately.
    return (all(self.program.solver.Solve({b}, self) for b in bindings)
            and self.program.solver.Solve(bindings, self))

  def RegisterBinding(self, binding):
    self.bindings.add(binding)

  def Label(self):
    """Return a string containing the node name and id."""
    return "<%d>%s" % (self.id, self.name)

  def __repr__(self):
    return "<cfgnode %d %s>" % (self.id, self.name)

  def AsciiTree(self, forward=False):
    """Draws an ascii tree, starting at this node.

    Args:
      forward: If True, draw the tree starting at this node. If False, draw
        the "reverse" tree that starts at the current node when following all
        edges in the reverse direction.
        The default is False, because during CFG construction, the current node
        typically doesn't have any outgoing nodes.
    Returns:
      A string.
    """
    if forward:
      return pytype.utils.ascii_tree(self, lambda node: node.outgoing)
    else:
      return pytype.utils.ascii_tree(self, lambda node: node.incoming)


class SourceSet(frozenset):
  """A SourceSet is a combination of Bindings that was used to form a Binding.

  In this context, a "source" is a Binding that was used to create another
  Binding.  E.g., for a statement like "z = a.x + y", a, a.x and y would be the
  Sources to create z, and would form a SourceSet.
  """
  __slots__ = ()


class Origin(collections.namedtuple("_", "where, source_sets")):
  """An "origin" is an explanation of how a binding was constructed.

  It consists of a CFG node and a set of sourcesets.

  Attributes:
    where: The CFG node where this assignment happened.
    source_sets: Possible SourceSets used to construct the binding we belong to.
      A set of SourceSet instances.
  """
  __slots__ = ()

  def __new__(cls, where, source_sets=None):
    return super(Origin, cls).__new__(
        cls, where, source_sets or set())

  def AddSourceSet(self, source_set):
    """Add a new possible source set."""
    self.source_sets.add(SourceSet(source_set))


class Binding(object):
  """A Binding assigns a binding to a (specific) variable.

  Bindings will therefore be stored in a dictionary in the Variable class,
  mapping strings to Binding instances.
  Depending on context, a Binding might also be called a "Source" (if it's
  used for creating another binding) or a "goal" (if we want to find a solution
  for a path through the program that assigns it).

  A binding has history ("origins"): It knows where the binding was
  originally retrieved from, before being assigned to something else here.
  Origins contain, through source_sets, "sources", which are other bindings.
  """
  __slots__ = ("program", "variable", "origins", "data", "_cfgnode_to_origin")

  def __init__(self, program, variable, data):
    """Initialize a new Binding. Usually called through Variable.AddBinding."""
    self.program = program
    self.variable = variable
    self.origins = []
    self.data = data
    self._cfgnode_to_origin = {}

  def IsVisible(self, viewpoint):
    """Can we "see" this binding from the current cfg node?

    This will run a solver to determine whether there's a path through the
    program that makes our variable have this binding at the given CFG node.

    Arguments:
      viewpoint: The CFG node at which this binding is possible / not possible.

    Returns:
      True if there is at least one path through the program
      in which the binding was assigned (and not overwritten afterwards), and
      all the bindings it depends on were assigned (and not overwritten) before
      that, etc.
    """
    self.program.CreateSolver()
    return self.program.solver.Solve({self}, viewpoint)

  def _FindOrAddOrigin(self, cfg_node):
    try:
      origin = self._cfgnode_to_origin[cfg_node]
    except KeyError:
      origin = Origin(cfg_node)
      self.origins.append(origin)
      self._cfgnode_to_origin[cfg_node] = origin
      self.variable.RegisterBindingAtNode(self, cfg_node)
      cfg_node.RegisterBinding(self)
    return origin

  def FindOrigin(self, cfg_node):
    """Return an Origin instance for a CFGNode, or None."""
    return self._cfgnode_to_origin.get(cfg_node)

  def AddOrigin(self, where, source_set):
    """Add another possible origin to this binding."""
    self.program.InvalidateSolver()
    origin = self._FindOrAddOrigin(where)
    origin.AddSourceSet(source_set)

  def AssignToNewVariable(self, where):
    """Assign this binding to a new variable."""
    variable = self.program.NewVariable()
    binding = variable.AddBinding(self.data)
    binding.AddOrigin(where, {self})
    return variable

  def HasSource(self, binding):
    """Does this binding depend on a given source?"""
    if self is binding:
      return True
    for origin in self.origins:
      for source_set in origin.source_sets:
        for source in source_set:
          if source.HasSource(binding):
            return True
    return False

  def __str__(self):
    data_id = getattr(self.data, "id", id(self.data))
    return "<binding of variable %d to data %d>" % (self.variable.id, data_id)

  __repr__ = __str__


class Variable(object):
  """A collection of possible bindings for a variable, along with their origins.

  A variable stores the bindings it can have as well as the CFG nodes at which
  the bindings occur. Callback functions can be registered with it, to be
  executed when a binding is added. The bindings are stored in a list for
  determinicity; new bindings should be added via AddBinding or
  (FilterAnd)PasteVariable rather than appended to bindings directly to ensure
  that bindings and _data_id_to_bindings are updated together. We do this rather
  than making _data_id_to_binding a collections.OrderedDict because a CFG can
  easily have tens of thousands of variables, and it takes about 40x as long to
  create an OrderedDict instance as to create a list and a dict, while adding a
  binding to the OrderedDict takes 2-3x as long as adding it to both the list
  and the dict.
  """
  __slots__ = ("program", "id", "bindings", "_data_id_to_binding",
               "_cfgnode_to_bindings", "_callbacks")

  def __init__(self, program, variable_id):
    """Initialize a new Variable. Called through Program.NewVariable."""
    self.program = program
    self.id = variable_id
    self.bindings = []
    self._data_id_to_binding = {}
    self._cfgnode_to_bindings = collections.defaultdict(set)
    self._callbacks = []

  def __repr__(self):
    return "<Variable v%d: %d choices>" % (
        self.id, len(self.bindings))

  __str__ = __repr__

  def Bindings(self, viewpoint):
    """Filters down the possibilities of bindings for this variable.

    It determines this by analyzing the control flow graph. Any definition for
    this variable that is invisible from the current point in the CFG is
    filtered out. This function differs from Filter() in that it only honors the
    CFG, not the source sets. As such, it's much faster.

    Arguments:
      viewpoint: The CFG node at which to determine the possible bindings.

    Returns:
      A filtered list of bindings for this variable.
    """
    num_bindings = len(self.bindings)
    if (viewpoint is None or
        ((len(self._cfgnode_to_bindings) == 1 or num_bindings == 1) and
         any(n in viewpoint.reachable_subset
             for n in self._cfgnode_to_bindings))):
      return self.bindings
    result = set()
    seen = set()
    stack = [viewpoint]
    while stack:
      if len(result) == num_bindings:
        break
      node = stack.pop()
      seen.add(node)
      # _cfgnode_to_bindings is a defaultdict, so don't use "get"
      if node in self._cfgnode_to_bindings:
        bindings = self._cfgnode_to_bindings[node]
        assert bindings, "empty binding list"
        result.update(bindings)
        # Don't expand this node - previous assignments to this variable will
        # be invisible, since they're overwritten here.
        continue
      else:
        stack.extend(set(node.incoming) - seen)
    return result

  def Data(self, viewpoint):
    """Like Bindings(cfg_node), but only return the data."""
    return [binding.data for binding in self.Bindings(viewpoint)]

  def Filter(self, viewpoint):
    """Filters down the possibilities of this variable.

    It analyzes the control flow graph. Any definition for this variable that is
    impossible at the current point in the CFG is filtered out.

    Arguments:
      viewpoint: The CFG node at which to determine the possible bindings.

    Returns:
      A filtered list of bindings for this variable.
    """
    return [b for b in self.bindings if b.IsVisible(viewpoint)]

  def FilteredData(self, viewpoint):
    """Like Filter(viewpoint), but only return the data."""
    return [b.data for b in self.bindings if b.IsVisible(viewpoint)]

  def _FindOrAddBinding(self, data):
    """Add a new binding if necessary, otherwise return existing binding."""
    if (len(self.bindings) >= MAX_VAR_SIZE - 1 and
        id(data) not in self._data_id_to_binding):
      data = self.program.default_data
    try:
      binding = self._data_id_to_binding[id(data)]
    except KeyError:
      self.program.InvalidateSolver()
      binding = Binding(self.program, self, data)
      self.bindings.append(binding)
      self._data_id_to_binding[id(data)] = binding
      for callback in self._callbacks:
        callback()
      _variable_size_metric.add(len(self.bindings))
    return binding

  def AddBinding(self, data, source_set=None, where=None):
    """Add another choice to this variable.

    This will not overwrite this variable in the current CFG node.  (It's
    legitimate to have multiple bindings for a variable on the same CFG node,
    e.g. if a union type is introduced at that node.)

    Arguments:
      data: A user specified object to uniquely identify this binding.
      source_set: An instance of SourceSet, i.e. a set of instances of Origin.
      where: Where in the CFG this variable was assigned to this binding.

    Returns:
      The new binding.
    """
    assert not isinstance(data, Variable)
    binding = self._FindOrAddBinding(data)
    if source_set or where:
      assert source_set is not None and where is not None
      binding.AddOrigin(where, source_set)
    return binding

  def PasteVariable(self, variable, where):
    """Adds all the bindings from another variable to this one."""
    for binding in variable.bindings:
      copy = self.AddBinding(binding.data)
      if all(origin.where == where for origin in binding.origins):
        # Optimization: If all the bindings of the old variable happen at the
        # same CFG node as the one we're assigning now, we can copy the old
        # source_set instead of linking to it. That way, the solver has to
        # consider fewer levels.
        for origin in binding.origins:
          for source_set in origin.source_sets:
            copy.AddOrigin(origin.where, source_set)
      else:
        copy.AddOrigin(where, {binding})

  def AssignToNewVariable(self, where):
    """Assign this variable to a new variable.

    This is essentially a copy: All entries in the Union will be copied to
    the new variable, but with the corresponding current variable binding
    as an origin.

    Arguments:
      where: CFG node where the assignment happens.

    Returns:
      A new variable.
    """
    new_variable = self.program.NewVariable()
    for binding in self.bindings:
      new_binding = new_variable.AddBinding(binding.data)
      new_binding.AddOrigin(where, {binding})
    return new_variable

  def RegisterBindingAtNode(self, binding, node):
    self._cfgnode_to_bindings[node].add(binding)

  def RegisterChangeListener(self, callback):
    self._callbacks.append(callback)

  def UnregisterChangeListener(self, callback):
    self._callbacks.remove(callback)

  @property
  def data(self):
    return [binding.data for binding in self.bindings]

  @property
  def nodes(self):
    return set(self._cfgnode_to_bindings)


def _GoalsConflict(goals):
  """Are the given bindings conflicting?

  Args:
    goals: A list of goals.

  Returns:
    True if we would need a variable to be assigned to two distinct
    bindings at the same time in order to solve this combination of goals.
    False if there are no conflicting goals.

  Raises:
    AssertionError: For internal errors.
  """
  variables = {}
  for goal in goals:
    existing = variables.get(goal.variable)
    if existing:
      if existing is goal:
        raise AssertionError("Internal error. Duplicate goal.")
      if existing.data is goal.data:
        raise AssertionError("Internal error. Duplicate data across bindings")
      return True
    variables[goal.variable] = goal
  return False


class State(object):
  """A state needs to "solve" a list of goals to succeed.

  Attributes:
    pos: Our current position in the CFG.
    goals: A list of bindings we'd like to be valid at this position.
  """
  __slots__ = ("pos", "goals")

  def __init__(self, pos, goals):
    """Initialize a state that starts at the given cfg node."""
    assert all(isinstance(goal, Binding) for goal in goals)
    self.pos = pos
    self.goals = set(goals)  # Make a copy. We modify these.

  def Done(self):
    """Is this State solved? This checks whether the list of goals is empty."""
    return not self.goals

  def NodesWithAssignments(self):
    """Find all CFG nodes corresponding to goal variable assignments.

    Mark all nodes that assign any of the goal variables (even to bindings not
    specified in goals).  This is used to "block" all cfg nodes that are
    conflicting assignments for the set of bindings in state.

    Returns:
      A set of instances of CFGNode. At every CFGNode in this set, at least
      one variable in the list of goals is assigned to something.
    """
    return set.union(*(goal.variable.nodes for goal in self.goals))

  def Replace(self, goal, replace_with):
    """Replace a goal with new goals (the origins of the expanded goal)."""
    assert goal in self.goals, "goal to expand not in state"
    self.goals.remove(goal)
    self.goals.update(replace_with)

  def _AddSources(self, goal, new_goals):
    """If the goal is trivially fulfilled, add its sources as new goals.

    Args:
      goal: The goal.
      new_goals: The set of new goals, to which goal's sources are added iff
        this method returns True.

    Returns:
      True if the goal is trivially fulfilled and False otherwise.
    """
    origin = goal.FindOrigin(self.pos)
    # For source sets > 2, we don't know which sources to use, so we have
    # to let the solver iterate over them later.
    if origin and len(origin.source_sets) <= 1:
      source_set, = origin.source_sets  # we always have at least one.
      new_goals.update(source_set)
      return True
    return False

  def RemoveFinishedGoals(self):
    """Remove all goals that are trivially fulfilled at the current CFG node."""
    new_goals = set()
    goals_to_remove = set()
    for goal in self.goals:
      if self._AddSources(goal, new_goals):
        goals_to_remove.add(goal)
    # We might remove multiple layers of nested goals, so loop until we don't
    # find anything to replace anymore. Storing new goals in a separate set is
    # faster than adding and removing them from self.goals.
    seen_goals = self.goals.copy()
    while new_goals:
      goal = new_goals.pop()
      if goal in seen_goals:
        # Only process a given goal once, to prevent infinite loops for cyclic
        # data structures.
        continue
      seen_goals.add(goal)
      if self._AddSources(goal, new_goals):
        goals_to_remove.add(goal)
      else:
        self.goals.add(goal)
    for goal in goals_to_remove:
      self.goals.discard(goal)
    return goals_to_remove

  def __hash__(self):
    """Compute hash for this State. We use States as keys when memoizing."""
    return hash(self.pos) + hash(frozenset(self.goals))

  def __eq__(self, other):
    return self.pos == other.pos and self.goals == other.goals

  def __ne__(self, other):
    return not self == other


class _PathFinder(object):
  """Finds a path between two nodes and collects nodes with conditions."""

  def __init__(self):
    self._solved_find_queries = {} # Cached queries

  def _ResetState(self):
    """Prepare the working state for a new search."""
    # A set of the nodes which have been on all paths so far.
    # In the end this will contain the intersection of all paths between
    # start and finish.
    self._solution_set = None
    # One path from start to finish. The nodes need to be returned ordered.
    # As otherwise we might set the destination behind the definition point of
    # some binding.
    self._one_path = None
    # A map from nodes to a set of nodes, which are on all paths between the
    # node and the finish.
    # If this is None it means that no path to finish exist from the node.
    self._node_to_finish_set = {}

  def _FindPathToNode(self, start, finish, blocked):
    """Determine whether we can reach a node without traversing conditions."""
    stack = [start]
    seen = set()
    while stack:
      node = stack.pop()
      if node is finish:
        return True
      if node in seen or node in blocked:
        continue
      seen.add(node)
      stack.extend(node.incoming)
    return False

  def FindNodeBackwards(self, start, finish, blocked):
    """Determine whether we can reach a CFG node, going backwards.

    Traverse the CFG from a starting point to find a given node, but avoid any
    nodes marked as "blocked".

    Arguments:
      start: Start node.
      finish: Node we're looking for.
      blocked: A set of blocked nodes. We do not consider start or finish to be
        blocked even if they apppear in this set.

    Returns:
      A tuple of Boolean and List[Binding]. The boolean is true iff a path exist
      from start to finish. The list contains all the nodes on a way between
      start and finish that contain a condition and are on all paths. The list
      is ordered by reachability from start. The list will be None if no path
      exist.
    """
    # Cache
    query = (start, finish, blocked)
    if query in self._solved_find_queries:
      return self._solved_find_queries[query]

    # Special case start.
    if start is finish:
      return (True, [start] if start.condition else [])

    # Run a quick DFS first, maybe we find a path that doesn't need a condition.
    if not self._FindPathToNode(start, finish, blocked):
      self._solved_find_queries[query] = False, None
      return self._solved_find_queries[query]

    self._ResetState()
    result = self._FindNodeBackwardsImpl(start, finish, blocked)
    self._solved_find_queries[query] = result
    return result

  def _FindNodeBackwardsImpl(self, start, finish, blocked):
    """Find a path from start to finish and return nodes on all such paths.

    If a node is on all paths between start and finish it's condition must be
    fullfiled for finish to be reachable.

    Args:
      start: The CFGNode from which to search.
      finish: The CFGNode we try to reach.
      blocked: A list of nodes we're not allowed to use.

    Returns:
      None, the result is saved in self._solution_set (nodes on all paths) and
      self._one_path (order of the nodes).
    """
    # Representation of the recursion state.
    path = [start]

    # Nodes which have already been added to a path.
    seen_set = set()
    node_to_iter = {}  # maps nodes to the iterator over their incoming links.
    while path:
      head = path[-1]
      if head not in node_to_iter:
        node_to_iter[head] = iter(head.incoming)
      it = node_to_iter[head]
      try:
        next_node = it.next()
      except StopIteration:
        path.pop()
        if head is finish:
          self._FinishNode(head, path)
        else:
          self._UpdateNodeToFinishSet(head, path)
        continue
      if next_node is finish:
        if self._solution_set is not None and not self._solution_set:
          # Solution set can never grow and is already empty.
          break
        self._FinishNode(next_node, path)
        continue
      if next_node in blocked:
        # The finish node is always blocked, therefore this needs to be below
        # the finish test.
        continue
      if next_node in seen_set:
        continue
      seen_set.add(next_node)
      path.append(next_node)

    if self._solution_set is not None:
      # self._one_path is a path from start to finish and self._solution_set is
      # the intersection of all those paths.
      # Order is important here, as the solver sets the first element of the
      # list to be the new destination and could jump over a destination if the
      # return value was unordered.
      return True, [node for node in self._one_path
                    if node.condition and node in self._solution_set]
    else:
      return False, None

  def _FinishNode(self, node, path):
    """Update the state with the results from one node.

    Args:
      node: The node to be analyzed.
      path: The path to this node, as a list of nodes.
    """
    this_path = list(path)
    this_path.append(node)
    if not self._one_path:
      self._one_path = this_path
    self._UpdateSolutionSet(this_path)
    # This is finish so the parent does not need to care about others
    self._node_to_finish_set[node] = {node}

  def _UpdateNodeToFinishSet(self, node, path):
    """Update the _node_to_finish_set for one node."""
    to_finish_set = None
    for incoming in node.incoming:
      # The whole algorithm uses DFS therfore we know that either incoming is
      # in self._node_to_finish_set or no path from incoming to finish exists.
      incoming_to_finish_set = self._node_to_finish_set.get(incoming)
      if incoming_to_finish_set is not None:
        if to_finish_set is None:
          to_finish_set = incoming_to_finish_set
        else:
          to_finish_set = to_finish_set.intersection(incoming_to_finish_set)

    if to_finish_set is not None:
      to_finish_set.add(node)
      nodes_on_path = list(path)
      nodes_on_path.extend(to_finish_set)
      self._UpdateSolutionSet(nodes_on_path)
    # Notice that if to_finish_set is not None, node has been added to this set.
    self._node_to_finish_set[node] = to_finish_set

  def _UpdateSolutionSet(self, new_path):
    if self._solution_set is None:
      self._solution_set = set(node for node in new_path if node.condition)
    else:
      self._solution_set = self._solution_set.intersection(new_path)


class Solver(object):
  """The solver class is instantiated for a given "problem" instance.

  It maintains a cache of solutions for subproblems to be able to recall them if
  they reoccur in the solving process.
  """

  _cache_metric = metrics.MapCounter("cfg_solver_cache")
  _goals_per_find_metric = metrics.Distribution("cfg_solver_goals_per_find")

  def __init__(self, program):
    """Initialize a solver instance. Every instance has their own cache.

    Arguments:
      program: The program we're in.
    """
    self.program = program
    self._solved_states = {}
    self._path_finder = _PathFinder()

  def Solve(self, start_attrs, start_node):
    """Try to solve the given problem.

    Try to prove one or more bindings starting (and going backwards from) a
    given node, all the way to the program entrypoint.

    Arguments:
      start_attrs: The assignments we're trying to have, at the start node.
      start_node: The CFG node where we want the assignments to be active.

    Returns:
      True if there is a path through the program that would give "start_attr"
      its binding at the "start_node" program position. For larger programs,
      this might only look for a partial path (i.e., a path that doesn't go
      back all the way to the entry point of the program).
    """
    state = State(start_node, start_attrs)
    return self._RecallOrFindSolution(state)

  def _RecallOrFindSolution(self, state):
    """Memoized version of FindSolution()."""
    if state in self._solved_states:
      Solver._cache_metric.inc("hit")
      return self._solved_states[state]

    # To prevent infinite loops, we insert this state into the hashmap as a
    # solvable state, even though we have not solved it yet. The reasoning is
    # that if it's possible to solve this state at this level of the tree, it
    # can also be solved in any of the children.
    self._solved_states[state] = True

    Solver._cache_metric.inc("miss")
    result = self._solved_states[state] = self._FindSolution(state)
    return result

  def _FindSolution(self, state):
    """Find a sequence of assignments that would solve the given state."""
    if state.Done():
      return True
    if _GoalsConflict(state.goals):
      return False
    Solver._goals_per_find_metric.add(len(state.goals))
    blocked = state.NodesWithAssignments()
    # We don't treat our current CFG node as blocked: If one of the goal
    # variables is overwritten by an assignment at our current pos, we assume
    # that assignment can still see the previous bindings.
    blocked.discard(state.pos)
    blocked = frozenset(blocked)
    # Find the goal cfg node that was assigned last.  Due to the fact that we
    # treat CFGs as DAGs, there's typically one unique cfg node with this
    # property.
    for goal in state.goals:
      # "goal" is the assignment we're trying to find.
      for origin in goal.origins:
        path_exist, path = self._path_finder.FindNodeBackwards(
            state.pos, origin.where, blocked)
        if path_exist:
          # This loop over multiple different combinations of origins is why
          # we need memoization of states.
          for source_set in origin.source_sets:
            new_goals = set(state.goals)
            for node in path:
              new_goals.add(node.condition)
            if path and len(new_goals) > len(state.goals):
              # If a goal was added, the binding for it might not yet exist at
              # origin.where, as it might have only been created after that
              # point in the CFG. Therefore the new destination needs to be the
              # point where the condition was defined. This must be before
              # origin.where as _FindNodeBackwards was able to find
              # origin.where going backwards from it.
              where = path[0]
            else:
              where = origin.where
            new_state = State(where, new_goals)
            if origin.where == new_state.pos:
              # The goal can only be replaced if origin.where was actually
              # reached.
              new_state.Replace(goal, source_set)

            # Also remove all goals that are trivially fulfilled at the
            # new CFG node.
            removed = new_state.RemoveFinishedGoals()
            removed.add(goal)
            if _GoalsConflict(removed):
              # Sometimes, we bulk-remove goals that are internally conflicting.
              return False
            if self._RecallOrFindSolution(new_state):
              return True
    return False
