from .sentinel import NOT_FOUND
import inspect
from .argextract import KeyExtractor, ClassKeyExtractor, NameKeyExtractor
from .error import RegistrationError


class Predicate(object):
    """A dispatch predicate.
    """
    def __init__(self, create_index, permutations,
                 get_key=None,
                 fallback=None):
        """
        :param create_index: a function that constructs an index;
          typically you supply either a :class:`KeyIndex` or
          :class:`ClassIndex`.
        :param permutations: a function that can construct an iterable of
           permutations for a predicate_key, from most specific to least
           specific.
        :param get_key: optional :class:`KeyExtractor`.
        :param fallback: optional fallback value. This value is returned
          if this is the most generic index for which no values could be
          found.
        """
        self.create_index = create_index
        self.permutations = permutations
        self.get_key = get_key
        self._fallback = fallback

    def argnames(self):
        """argnames that this predicate needs to dispatch on.
        """
        if self.get_key is None:
            return set()
        return set(self.get_key.names)

    def fallback(self, index, key):
        """Return fallback if this index does not contain key.

        If index contains no permutations of key, then ``NOT_FOUND``
        is returned.
        """
        for k in self.permutations(key):
            if index.get(k, NOT_FOUND) is not NOT_FOUND:
                return NOT_FOUND
        return self._fallback


def key_predicate(get_key=None, fallback=None):
    """Construct predicate indexed on any immutable value.

    :get_key: a :class:`KeyExtractor`. Should return key to dispatch on.
    :fallback: a fallback value. By default is ``None``.
    :returns: a :class:`Predicate`.
    """
    return Predicate(KeyIndex, key_permutations, get_key, fallback)


def key_permutations(key):
    """Permutations for a simple immutable key.

    There is only a single permutation: the key itself.
    """
    yield key


def class_predicate(get_key=None, fallback=None):
    """Construct predicate indexed on class.

    :get_key: a :class:`KeyExtractor`. Should return class to dispatch on.
    :fallback: a fallback value. By default is ``None``.
    :returns: a :class:`Predicate`.
    """
    return Predicate(KeyIndex, class_permutations, get_key, fallback)


def class_permutations(key):
    """Permutations for class key.

    Returns class and its based in mro order. If a classic class in
    Python 2, smuggle in ``object`` as the base class anyway to make
    lookups consistent.
    """
    for class_ in inspect.getmro(key):
        yield class_
    if class_ is not object:
        yield object


def match_key(func, fallback=None):
    """Predicate that extracts immutable key according to func.

    :func: argument that takes arguments. These arguments are
      extracted from the arguments given to the dispatch function.
      This function should return what to dispatch on.
    :fallback: the fallback value. By default it is ``None``.
    :returns: a :class:`Predicate`.
    """
    return key_predicate(KeyExtractor(func), fallback)


def match_instance(func, fallback=None):
    """Predicate that extracts class of instance returned by func.

    :func: argument that takes arguments. These arguments are
      extracted from the arguments given to the dispatch function.
      This function should return an instance; dispatching is done
      on the class of that instance.
    :fallback: the fallback value. By default it is ``None``.
    :returns: a :class:`Predicate`.
    """
    return class_predicate(ClassKeyExtractor(func), fallback)


def match_argname(name, fallback=None):
    """Predicate that extracts class of specified argument.

    :name: name of the argument to dispatch on - its class will
      be used for the dispatch.
    :fallback: the fallback value. By default it is ``None``.
    :returns: a :class:`Predicate`.
    """
    return class_predicate(NameKeyExtractor(name), fallback)


def match_class(func, fallback=None):
    """Predicate that extracts class returned by func.

    :func: argument that takes arguments. These arguments are
      extracted from the arguments given to the dispatch function.
      This function should return a class; dispatching is done
      on this class.
    :fallback: the fallback value. By default it is ``None``.
    :returns: a :class:`Predicate`.
    """
    return class_predicate(KeyExtractor(func), fallback)


class MultiPredicate(object):
    def __init__(self, predicates):
        self.predicates = predicates

    def create_index(self):
        return MultiIndex(self.predicates)

    def permutations(self, key):
        return multipredicate_permutations(self.predicates, key)

    def get_key(self, d):
        return tuple([predicate.get_key(d) for predicate in self.predicates])

    def argnames(self):
        result = set()
        for predicate in self.predicates:
            result.update(predicate.argnames())
        return result

    def fallback(self, multi_index, key):
        for index, k, predicate in zip(multi_index.indexes,
                                       key, self.predicates):
            result = predicate.fallback(index, k)
            if result is not NOT_FOUND:
                return result
        return NOT_FOUND


class KeyIndex(object):
    def __init__(self):
        self.d = {}

    def add(self, key, value):
        self.d.setdefault(key, set()).add(value)

    def get(self, key, default=None):
        return self.d.get(key, default)


class MultiIndex(object):
    def __init__(self, predicates):
        self.predicates = predicates
        self.indexes = [predicate.create_index() for predicate in predicates]

    def add(self, keys, value):
        for index, key in zip(self.indexes, keys):
            index.add(key, value)

    def get(self, keys, default):
        matches = []
        # get all matching indexes first
        for index, key in zip(self.indexes, keys):
            match = index.get(key)
            # bail out early if None or any match has 0 items
            if not match:
                return default
            matches.append(match)
        # sort matches by length.
        # this allows cheaper intersection calls later
        matches.sort(key=lambda match: len(match))

        result = None
        for match in matches:
            if result is None:
                result = match
            else:
                result = result.intersection(match)
            # bail out early if there is nothing left
            if not result:
                return default
        return result


class PredicateRegistry(object):
    def __init__(self, predicate):
        self.known_keys = set()
        self.predicate = predicate
        self.index = self.predicate.create_index()

    def register(self, key, value):
        if key in self.known_keys:
            raise RegistrationError(
                "Already have registration for key: %s" % (key,))
        self.index.add(key, value)
        self.known_keys.add(key)

    def key(self, d):
        return self.predicate.get_key(d)

    def argnames(self):
        return self.predicate.argnames()

    def component(self, key):
        result = next(self.all(key), NOT_FOUND)
        if result is NOT_FOUND:
            return self.fallback(key)
        return result

    def fallback(self, key):
        return self.predicate.fallback(self.index, key)

    def all(self, key):
        for p in self.predicate.permutations(key):
            result = self.index.get(p, NOT_FOUND)
            if result is not NOT_FOUND:
                yield tuple(result)[0]


class SingleValueRegistry(object):
    def __init__(self):
        self.value = None

    def register(self, key, value):
        if self.value is not None:
            raise RegistrationError(
                "Already have registration for key: %s" % (key,))
        self.value = value

    def key(self, d):
        return ()

    def argnames(self):
        return set()

    def component(self, key):
        return self.value

    def all(self, key):
        yield self.value


# XXX transform to non-recursive version
# use # http://blog.moertel.com/posts/2013-05-14-recursive-to-iterative-2.html
def multipredicate_permutations(predicates, keys):
    first = keys[0]
    rest = keys[1:]
    first_predicate = predicates[0]
    rest_predicates = predicates[1:]
    if not rest:
        for permutation in first_predicate.permutations(first):
            yield (permutation,)
        return
    for permutation in first_predicate.permutations(first):
        for rest_permutation in multipredicate_permutations(
                rest_predicates, rest):
            yield (permutation,) + rest_permutation
