from __future__ import unicode_literals
from functools import update_wrapper, partial
from .predicate import match_argname
from .compat import string_types
from .argextract import ArgExtractor
from .predicate import create_predicates_registry
from .arginfo import arginfo
from .error import RegistrationError


class dispatch(object):
    """Decorator to make a function dispatch based on its arguments.

    This takes the predicates to dispatch on as zero or more parameters.

    :param predicates: sequence of :class:`Predicate` instances
      to do the dispatch on. You create predicates using
      :func:`reg.match_instance`, :func:`reg.match_key`,
      :func:`reg.match_class`, or :func:`reg.match_argname`, or with a
      custom predicate class.

      You can also pass in plain string argument, which is turned into
      a :func:`reg.match_instance` predicate.
    :param get_key_lookup: a function that gets a :class:`PredicateRegistry`
      instance and returns a key lookup. A :class:`PredicateRegistry` instance
      is itself a key lookup, but you can return :class:`reg.CachingKeyLookup`
      to make it more efficient.
    :returns: a :class:`reg.Dispatch` instance.
    """
    def __init__(self, *predicates, **kw):
        self.predicates = [self._make_predicate(predicate)
                           for predicate in predicates]
        self.get_key_lookup = kw.pop('get_key_lookup', identity)

    def _make_predicate(self, predicate):
        if isinstance(predicate, string_types):
            return match_argname(predicate)
        return predicate

    def __call__(self, callable):
        return Dispatch(self.predicates, callable, self.get_key_lookup)


def identity(registry):
    return registry


class Dispatch(object):
    """Dispatch function.

    You can register implementations based on particular predicates. The
    dispatch function dispatches to these implementations based on its
    arguments.

    :param predicates: a list of predicates.
    :param callable: the Python function object to register dispatch
      implementations for. The signature of an implementation needs to
      match that of this function. This function is used as a fallback
      implementation that is called if no specific implementations match.
    :param get_key_lookup: a function that gets a :class:`PredicateRegistry`
      instance and returns a key lookup. A :class:`PredicateRegistry` instance
      is itself a key lookup, but you can return :class:`reg.CachingKeyLookup`
      to make it more efficient.
    """
    def __init__(self, predicates, callable, get_key_lookup):
        self.wrapped_func = callable
        self.get_key_lookup = get_key_lookup
        self._original_class = type(self)
        self._original_predicates = predicates
        self._register_predicates(predicates)
        update_wrapper(self, callable)

    def _register_predicates(self, predicates):
        self.registry = create_predicates_registry(predicates)
        self.predicates = predicates
        self.key_lookup = self.get_key_lookup(self.registry)
        self.arg_extractor = ArgExtractor(
            self.wrapped_func, self.registry.argnames())

        # We build the __call__ method on the fly. The body of
        # __call__ uses the identifiers defined in the following
        # namespace:
        namespace = {
            '_registry_key': self.registry.key,
            '_component_lookup': self.key_lookup.component,
            '_fallback_lookup': self.key_lookup.fallback,
            '_fallback': self.wrapped_func,
            }

        # The definition of __call__ requires the signature of the
        # wrapped function and the arguments needed by the registered
        # predicates (predicate_args):
        code_template = """\
def {name}(_self, {signature}):
    _key = _registry_key(dict({predicate_args}))
    return (_component_lookup(_key) or
            _fallback_lookup(_key) or
            _fallback)({signature})
"""

        args = arginfo(self.wrapped_func)
        name = self.wrapped_func.__name__
        signature = ', '.join(
            args.args +
            (['*' + args.varargs] if args.varargs else []) +
            (['**' + args.keywords] if args.keywords else []))
        code_source = code_template.format(
            name=name,
            signature=signature,
            predicate_args=', '.join(
                '{0}={0}'.format(x) for x in self.registry.argnames()))

        # We now compile __call__ to byte-code:
        exec(code_source, namespace)
        call = namespace[name]

        # We copy over the defaults from the wrapped function.
        call.__defaults__ = args.defaults

        # Unfortunately __call__ must be a method, we cannot simply
        # assign it to self.__call__.  So we have to create a one-off
        # class and set it as the new type of self:
        self.__class__ = type(
            self._original_class.__name__,
            (self._original_class,),
            {'__call__': call})

    def clean(self):
        """Clean up implementations and added predicates.

        This restores the dispatch function to its original state,
        removing registered implementations and predicates added
        using :meth:`reg.Dispatch.add_predicates`.
        """
        self._register_predicates(self._original_predicates)

    def add_predicates(self, predicates):
        """Add new predicates.

        Extend the predicates used by this predicates. This can be
        used to add predicates that are configured during startup time.

        Note that this clears up any registered implementations.

        :param predicates: a list of predicates to add.
        """
        self._register_predicates(self.predicates + predicates)

    def register(self, func=None, **key_dict):
        """Register an implementation.

        If ``func`` is not specified, this method can be used as a
        decorator and the decorated function will be used as the
        actual ``func`` argument.

        :param func: a function that implements behavior for this
          dispatch function. It needs to have the same signature as
          the original dispatch function. If this is a
          :class:`reg.DispatchMethod`, then this means it needs to
          take a first context argument.
        :param key_dict: keyword arguments describing the registration,
          with as keys predicate name and as values predicate values.
        :returns: ``func``.
        """
        if func is None:
            return partial(self.register, **key_dict)
        validate_signature(func, self.wrapped_func)
        predicate_key = self.registry.key_dict_to_predicate_key(key_dict)
        self.register_value(predicate_key, func)
        return func

    def register_value(self, predicate_key, value):
        """Low-level function to register a value.

        Can be used to register an arbitrary non-callable Python
        object. Of course this cannot be called, but you can still
        look it up using :meth:`reg.Dispatch.component`.
        """
        if isinstance(predicate_key, list):
            predicate_key = tuple(predicate_key)
        # if we have a 1 tuple, we register the single value inside
        if isinstance(predicate_key, tuple) and len(predicate_key) == 1:
            predicate_key = predicate_key[0]
        self.registry.register(predicate_key, value)

    def __repr__(self):
        return repr(self.wrapped_func)

    def predicate_key(self, *args, **kw):
        """Construct predicate_key for function arguments.

        For function arguments, construct the appropriate
        ``predicate_key``. This is used by the dispatch mechanism to
        dispatch to the right function.

        If the ``predicate_key`` cannot be constructed from ``args``
        and ``kw``, this raises a :exc:`KeyExtractorError`.

        :param args: the varargs given to the callable.
        :param kw: the keyword arguments given to the callable.
        :returns: an immutable ``predicate_key`` based on the predicates
          the callable was configured with.
        """
        return self.registry.key(self.arg_extractor(*args, **kw))

    def component(self, *args, **kw):
        """Lookup function dispatched to with args and kw.

        Looks up the function to dispatch to using args and
        kw. Returns the fallback value (default: ``None``) if nothing
        could be found.

        :args: varargs. Used to extract dispatch information to
           construct ``predicate_key``.
        :kw: keyword arguments. Used to extract
           dispatch information to construct ``predicate_key``.
        :returns: the function being dispatched to, or None.
        """
        key = self.predicate_key(*args, **kw)
        return self.key_lookup.component(key)

    def fallback(self, *args, **kw):
        """Lookup fallback for args and kw.

        :args: varargs. Used to extract dispatch information to
           construct ``predicate_key``.
        :kw: keyword arguments. Used to extract
           dispatch information to construct ``predicate_key``.
        :returns: the function being dispatched to, or fallback.
        """
        key = self.predicate_key(*args, **kw)
        return self.key_lookup.fallback(key)

    def component_by_keys(self, **kw):
        """Look up function based on key_dict.

        Looks up the function to dispatch to using a key_dict,
        mapping predicate name to predicate value. Returns the fallback
        value (default: ``None``) if nothing could be found.

        :kw: key is predicate name, value is
          predicate value under which it was registered.
          If omitted, predicate default is used.
        :returns: the function being dispatched to, or fallback.
        """
        key = self.key_lookup.key_dict_to_predicate_key(kw)
        return self.key_lookup.component(key)

    def all(self, *args, **kw):
        """Lookup all functions dispatched to with args and kw.

        Looks up functions for all permutations based on predicate_key,
        where predicate_key is constructed from args and kw.

        :args: varargs. Used to extract dispatch information to
           construct predicate_key.
        :kw: keyword arguments. Used to extract
           dispatch information to construct predicate_key.
        :returns: an iterable of functions.
        """
        key = self.predicate_key(*args, **kw)
        return self.key_lookup.all(key)

    def all_by_keys(self, **kw):
        """Look up all functions dispatched to using keyword arguments.

        Looks up the function to dispatch to using a ``key_dict``,
        mapping predicate name to predicate value. Returns the fallback
        value (default: ``None``) if nothing could be found.

        :kw: a dictionary. key is predicate name, value is
          predicate value. If omitted, predicate default is used.
        :returns: iterable of functions being dispatched to.
        """
        key = self.key_lookup.key_dict_to_predicate_key(kw)
        return self.key_lookup.all(key)

    def key_dict_to_predicate_key(self, key_dict):
        """Turn a key dict into a predicate key.

        Given a key dict under which an implementation function is
        registered, return an immutable predicate key.

        :param key_dict: dict with registration information
        :returns: an immutable predicate key
        """
        return self.registry.key_dict_to_predicate_key(key_dict)


def validate_signature(f, dispatch):
    f_arginfo = arginfo(f)
    if f_arginfo is None:
        raise RegistrationError(
            "Cannot register non-callable for dispatch "
            "%r: %r" % (dispatch, f))
    if not same_signature(arginfo(dispatch), f_arginfo):
        raise RegistrationError(
            "Signature of callable dispatched to (%r) "
            "not that of dispatch (%r)" % (
                f, dispatch))


def same_signature(a, b):
    """Check whether a arginfo and b arginfo are the same signature.

    Actual names of arguments may differ. Default arguments may be
    different.
    """
    a_args = set(a.args)
    b_args = set(b.args)
    return (len(a_args) == len(b_args) and
            a.varargs == b.varargs and
            a.keywords == b.keywords)
