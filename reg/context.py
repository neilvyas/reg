from __future__ import unicode_literals
import types
import inspect
from .compat import (create_method_for_class,
                     create_method_for_instance)
from .dispatch import dispatch, Dispatch, same_signature
from .arginfo import arginfo
from .error import RegistrationError


class dispatch_method(dispatch):
    """Decorator to make a method on a context class dispatch.

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
    :param auto_argument: argument name. if the first argument
      (context instance) registered with
      :meth:`reg.DispatchMethod.register_auto` has this name, it is
      registered as a method using :meth:`reg.Dispatch.register`,
      otherwise it is registered as a function using
      :meth:`reg.DispatchMethod.register_function`.
    :returns: a :class:`reg.DispatchMethod` instance.
    """
    def __init__(self, *predicates, **kw):
        super(dispatch_method, self).__init__(*predicates, **kw)

    def __call__(self, callable):
        return DispatchMethodDescriptor(callable,
                                        self.predicates,
                                        self.get_key_lookup)


class DispatchMethod(Dispatch):
    def __init__(self, predicates, callable, get_key_lookup,
                 auto_argument='app'):
        super(DispatchMethod, self).__init__(
            predicates, callable, get_key_lookup)
        self.auto_argument = auto_argument

    def register_function(self, func, **key_dict):
        """Register an implementation function.

        You can register a function that does not get the context
        instance as the first argument. This automatically wraps this
        function as a method, discarding its first (context) argument.

        :param func: a function that implements behavior for this
          dispatch function. It needs to have the same signature
          as the original dispatch method, without the first argument.
        :key_dict: keyword arguments describing the registration,
          with as keys predicate name and as values predicate values.
        """
        validate_signature_without_first_arg(func, self.wrapped_func)
        predicate_key = self.registry.key_dict_to_predicate_key(key_dict)
        self.register_value(predicate_key, methodify(func))

    def register_auto(self, func, **key_dict):
        """Register an implementation function or method.

        If the function you register has a first (context) argument
        with the same name as the ``auto_argument`` value you passed to
        :func:`reg.dispatch_method`, it is registered as a method
        using :meth:`reg.Dispatch.register`. Otherwise, it is registered
        as a function using :meth:`reg.DispatchMethod.register_function`.

        :param func: a function that implements behavior for this
          dispatch function. It needs to have the same signature
          as the original dispatch method, with optionally a first
          argument with name indicated by ``auto_argument``.
        :key_dict: keyword arguments describing the registration,
          with as keys predicate name and as values predicate values.
        """
        if is_auto_method(func, self.auto_argument):
            # for symmetry as register_function with a wrapped version
            # is possible, we also set the value
            func.value = func
            self.register(func, **key_dict)
        else:
            self.register_function(func, **key_dict)

    def component(self, *args, **kw):
        # pass in a None as the first argument
        # this matches up the bound self that is passed automatically
        # into __call__
        return super(DispatchMethod, self).component(None, *args, **kw)

    def fallback(self, *args, **kw):
        return super(DispatchMethod, self).fallback(None, *args, **kw)

    def all(self, *args, **kw):
        return super(DispatchMethod, self).all(None, *args, **kw)


class DispatchMethodDescriptor(object):
    def __init__(self, callable, predicates, get_key_lookup,
                 cache_bound_method=True):
        self.callable = callable
        self.name = self.callable.__name__
        self.predicates = predicates
        self.get_key_lookup = get_key_lookup
        self.cache_bound_method = cache_bound_method
        self._cache = {}

    def __get__(self, obj, type=None):
        # we get the method from the cache
        # this guarantees that we distinguish between dispatches
        # on a per class basis, and on the name of the method
        dispatch = self._cache.get(type)

        if dispatch is None:
            # if this is the first time we access the dispatch method,
            # we create it and store it in the cache
            dispatch = DispatchMethod(self.predicates,
                                      self.callable,
                                      self.get_key_lookup)
            self._cache[type] = dispatch

        # we cannot attach the dispatch method to the class
        # directly (skipping the descriptor during next access) here,
        # because we need to return a distinct dispatch for each
        # class, including subclasses.
        if obj is None:
            # we access it through the class directly, so unbound
            return create_method_for_class(dispatch, type)

        # if we access the instance, we simulate binding it
        bound = create_method_for_instance(dispatch, obj)
        # we store it on the instance, so that next time we
        # access this, we do not hit the descriptor anymore
        # but return the bound dispatch function directly
        if self.cache_bound_method:
            setattr(obj, self.name, bound)
        return bound


def validate_signature_without_first_arg(f, dispatch):
    f_arginfo = arginfo(f)
    if f_arginfo is None:
        raise RegistrationError(
            "Cannot register non-callable for dispatch "
            "%r: %r" % (dispatch, f))

    dispatch_arginfo = arginfo(dispatch)
    # strip off first argument (as this is self or cls)
    dispatch_arginfo = inspect.ArgInfo(
        dispatch_arginfo.args[1:],
        dispatch_arginfo.varargs,
        dispatch_arginfo.keywords,
        dispatch_arginfo.defaults)
    if not same_signature(dispatch_arginfo, f_arginfo):
        raise RegistrationError(
            "Signature of callable dispatched to (%r) "
            "not that of dispatch (without self) (%r)" % (
                f, dispatch))


def methodify(func):
    """Turn a function into a method.

    Wraps the function so that it takes a first argument like
    a method, and ignores it.

    The return value has a ``value`` attribute which is the original
    function that was wrapped. This way the application can access it.
    """
    def wrapped(self, *args, **kw):
        return func(*args, **kw)
    wrapped.value = func
    return wrapped


def install_auto_method(klass, name, func, auto_argument="app"):
    """Install func as method onto klass.

    Sometimes you don't need a full fledged dispatch method on a
    class, but setting up a simple function will do. This helps you
    set up this function with special respect for an automatic context
    argument along the lines of
    :meth:`reg.DispatchMethod.register_auto`.

    If func has a first argument that is named as ``auto_argument``,
    the func is bound as a method to the class.

    If func has no such first argument, a wrapper function is created
    that does take it, and is bound as a method to the class.

    :param klass: class to install the method on.
    :param name: method name.
    :param func: the callable to install as a method. If its first
      argument name is *not* ``auto_argument``, it is first wrapped
      into an object that does take a first argument, so that it can
      be installed as a method.
    :param auto_argument: the name of the first argument that indicates
      we want to install this directly as a method. If the first argument
      does not has this name, wrap the callable so that it does take
      that argument before installing it.

    """
    if is_auto_method(func, auto_argument):
        # for symmetry make sure value is set
        if not isinstance(func, types.FunctionType):
            # if this isn't a function, we wrap it first
            # this is helpful when we set it as a method for
            # compatibility with Python 2 and Python 3
            def result(self, *args, **kw):
                return func(self, *args, **kw)
            result.value = func
        else:
            # otherwise we store the value directly on the function
            func.value = func
            result = func
    else:
        # we create the function wrapper
        result = methodify(func)

    setattr(klass, name, result)


def is_auto_method(func, auto_argument="app"):
    """Check whether a function is already a method
    """
    info = arginfo(func)
    return info.args and info.args[0] == auto_argument


def clean_dispatch_methods(cls):
    """For a given class clean all dispatch methods.

    This resets their registry to the original state using
    :meth:`reg.DispatchMethod.clean`.

    :param cls: a class that has :class:`reg.DispatchMethod` methods on it.
    """
    for name in dir(cls):
        attr = getattr(cls, name)
        im_func = getattr(attr, '__func__', None)
        if im_func is None:
            continue
        if isinstance(im_func, DispatchMethod):
            attr.clean()