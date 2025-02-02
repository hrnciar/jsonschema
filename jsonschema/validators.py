"""
Creation and extension of validators, with implementations for existing drafts.
"""
from collections.abc import Sequence
from functools import lru_cache
from urllib.parse import unquote, urldefrag, urljoin, urlsplit
from urllib.request import urlopen
from warnings import warn
import contextlib
import json
import warnings

from jsonschema import (
    _legacy_validators,
    _types,
    _utils,
    _validators,
    exceptions,
)

validators = {}
meta_schemas = _utils.URIDict()
_VOCABULARIES = _utils.URIDict()


def __getattr__(name):
    if name == "ErrorTree":
        warnings.warn(
            "Importing ErrorTree from jsonschema.validators is deprecated. "
            "Instead import it from jsonschema.exceptions.",
            DeprecationWarning,
        )
        from jsonschema.exceptions import ErrorTree
        return ErrorTree
    raise AttributeError(f"module {__name__} has no attribute {name}")


def validates(version):
    """
    Register the decorated validator for a ``version`` of the specification.

    Registered validators and their meta schemas will be considered when
    parsing ``$schema`` properties' URIs.

    Arguments:

        version (str):

            An identifier to use as the version's name

    Returns:

        collections.abc.Callable:

            a class decorator to decorate the validator with the version
    """

    def _validates(cls):
        validators[version] = cls
        meta_schema_id = cls.ID_OF(cls.META_SCHEMA)
        meta_schemas[meta_schema_id] = cls

        for vocabulary in cls.VOCABULARY_SCHEMAS:
            vocabulary_id = cls.ID_OF(vocabulary)
            _VOCABULARIES[vocabulary_id] = vocabulary

        return cls
    return _validates


def _id_of(schema):
    if schema is True or schema is False:
        return ""
    return schema.get("$id", "")


def _store_schema_list():
    return [
        (id, validator.META_SCHEMA) for id, validator in meta_schemas.items()
    ] + [
        (id, schema) for id, schema in _VOCABULARIES.items()
    ]


def create(
    meta_schema,
    vocabulary_schemas=(),
    validators=(),
    version=None,
    type_checker=_types.draft7_type_checker,
    id_of=_id_of,
    applicable_validators=lambda schema: schema.items(),
):
    """
    Create a new validator class.

    Arguments:

        meta_schema (collections.abc.Mapping):

            the meta schema for the new validator class

        validators (collections.abc.Mapping):

            a mapping from names to callables, where each callable will
            validate the schema property with the given name.

            Each callable should take 4 arguments:

                1. a validator instance,
                2. the value of the property being validated within the
                   instance
                3. the instance
                4. the schema

        version (str):

            an identifier for the version that this validator class will
            validate. If provided, the returned validator class will
            have its ``__name__`` set to include the version, and also
            will have `jsonschema.validators.validates` automatically
            called for the given version.

        type_checker (jsonschema.TypeChecker):

            a type checker, used when applying the :validator:`type` validator.

            If unprovided, a `jsonschema.TypeChecker` will be created
            with a set of default types typical of JSON Schema drafts.

        id_of (collections.abc.Callable):

            A function that given a schema, returns its ID.

        applicable_validators (collections.abc.Callable):

            A function that given a schema, returns the list of applicable
            validators (names and callables) which will be called on to
            validate the instance.

    Returns:

        a new `jsonschema.IValidator` class
    """

    class Validator:

        VALIDATORS = dict(validators)
        META_SCHEMA = dict(meta_schema)
        VOCABULARY_SCHEMAS = list(vocabulary_schemas)
        TYPE_CHECKER = type_checker
        ID_OF = staticmethod(id_of)

        def __init__(self, schema, resolver=None, format_checker=None):
            if resolver is None:
                resolver = RefResolver.from_schema(schema, id_of=id_of)

            self.resolver = resolver
            self.format_checker = format_checker
            self.schema = schema

        @classmethod
        def check_schema(cls, schema):
            for error in cls(cls.META_SCHEMA).iter_errors(schema):
                raise exceptions.SchemaError.create_from(error)

        def iter_errors(self, instance, _schema=None):
            if _schema is None:
                _schema = self.schema

            if _schema is True:
                return
            elif _schema is False:
                yield exceptions.ValidationError(
                    f"False schema does not allow {instance!r}",
                    validator=None,
                    validator_value=None,
                    instance=instance,
                    schema=_schema,
                )
                return

            scope = id_of(_schema)
            if scope:
                self.resolver.push_scope(scope)
            try:
                validators = applicable_validators(_schema)
                for k, v in validators:
                    validator = self.VALIDATORS.get(k)
                    if validator is None:
                        continue

                    errors = validator(self, v, instance, _schema) or ()
                    for error in errors:
                        # set details if not already set by the called fn
                        error._set(
                            validator=k,
                            validator_value=v,
                            instance=instance,
                            schema=_schema,
                        )
                        if k not in {"if", "$ref"}:
                            error.schema_path.appendleft(k)
                        yield error
            finally:
                if scope:
                    self.resolver.pop_scope()

        def descend(self, instance, schema, path=None, schema_path=None):
            for error in self.iter_errors(instance, schema):
                if path is not None:
                    error.path.appendleft(path)
                if schema_path is not None:
                    error.schema_path.appendleft(schema_path)
                yield error

        def validate(self, *args, **kwargs):
            for error in self.iter_errors(*args, **kwargs):
                raise error

        def is_type(self, instance, type):
            try:
                return self.TYPE_CHECKER.is_type(instance, type)
            except exceptions.UndefinedTypeCheck:
                raise exceptions.UnknownType(type, instance, self.schema)

        def is_valid(self, instance, _schema=None):
            error = next(self.iter_errors(instance, _schema), None)
            return error is None

    if version is not None:
        Validator = validates(version)(Validator)
        Validator.__name__ = (
            version.title().replace(" ", "").replace("-", "") + "Validator"
        )

    return Validator


def extend(validator, validators=(), version=None, type_checker=None):
    """
    Create a new validator class by extending an existing one.

    Arguments:

        validator (jsonschema.IValidator):

            an existing validator class

        validators (collections.abc.Mapping):

            a mapping of new validator callables to extend with, whose
            structure is as in `create`.

            .. note::

                Any validator callables with the same name as an
                existing one will (silently) replace the old validator
                callable entirely, effectively overriding any validation
                done in the "parent" validator class.

                If you wish to instead extend the behavior of a parent's
                validator callable, delegate and call it directly in
                the new validator function by retrieving it using
                ``OldValidator.VALIDATORS["validator_name"]``.

        version (str):

            a version for the new validator class

        type_checker (jsonschema.TypeChecker):

            a type checker, used when applying the :validator:`type` validator.

            If unprovided, the type checker of the extended
            `jsonschema.IValidator` will be carried along.

    Returns:

        a new `jsonschema.IValidator` class extending the one provided

    .. note:: Meta Schemas

        The new validator class will have its parent's meta schema.

        If you wish to change or extend the meta schema in the new
        validator class, modify ``META_SCHEMA`` directly on the returned
        class. Note that no implicit copying is done, so a copy should
        likely be made before modifying it, in order to not affect the
        old validator.
    """

    all_validators = dict(validator.VALIDATORS)
    all_validators.update(validators)

    if type_checker is None:
        type_checker = validator.TYPE_CHECKER
    return create(
        meta_schema=validator.META_SCHEMA,
        validators=all_validators,
        version=version,
        type_checker=type_checker,
        id_of=validator.ID_OF,
    )


Draft3Validator = create(
    meta_schema=_utils.load_schema("draft3"),
    validators={
        "$ref": _validators.ref,
        "additionalItems": _validators.additionalItems,
        "additionalProperties": _validators.additionalProperties,
        "dependencies": _legacy_validators.dependencies_draft3,
        "disallow": _legacy_validators.disallow_draft3,
        "divisibleBy": _validators.multipleOf,
        "enum": _validators.enum,
        "extends": _legacy_validators.extends_draft3,
        "format": _validators.format,
        "items": _legacy_validators.items_draft3_draft4,
        "maxItems": _validators.maxItems,
        "maxLength": _validators.maxLength,
        "maximum": _legacy_validators.maximum_draft3_draft4,
        "minItems": _validators.minItems,
        "minLength": _validators.minLength,
        "minimum": _legacy_validators.minimum_draft3_draft4,
        "pattern": _validators.pattern,
        "patternProperties": _validators.patternProperties,
        "properties": _legacy_validators.properties_draft3,
        "type": _legacy_validators.type_draft3,
        "uniqueItems": _validators.uniqueItems,
    },
    type_checker=_types.draft3_type_checker,
    version="draft3",
    id_of=lambda schema: schema.get("id", ""),
    applicable_validators=_legacy_validators.ignore_ref_siblings,
)

Draft4Validator = create(
    meta_schema=_utils.load_schema("draft4"),
    validators={
        "$ref": _validators.ref,
        "additionalItems": _validators.additionalItems,
        "additionalProperties": _validators.additionalProperties,
        "allOf": _validators.allOf,
        "anyOf": _validators.anyOf,
        "dependencies": _legacy_validators.dependencies_draft4_draft6_draft7,
        "enum": _validators.enum,
        "format": _validators.format,
        "items": _legacy_validators.items_draft3_draft4,
        "maxItems": _validators.maxItems,
        "maxLength": _validators.maxLength,
        "maxProperties": _validators.maxProperties,
        "maximum": _legacy_validators.maximum_draft3_draft4,
        "minItems": _validators.minItems,
        "minLength": _validators.minLength,
        "minProperties": _validators.minProperties,
        "minimum": _legacy_validators.minimum_draft3_draft4,
        "multipleOf": _validators.multipleOf,
        "not": _validators.not_,
        "oneOf": _validators.oneOf,
        "pattern": _validators.pattern,
        "patternProperties": _validators.patternProperties,
        "properties": _validators.properties,
        "required": _validators.required,
        "type": _validators.type,
        "uniqueItems": _validators.uniqueItems,
    },
    type_checker=_types.draft4_type_checker,
    version="draft4",
    id_of=lambda schema: schema.get("id", ""),
    applicable_validators=_legacy_validators.ignore_ref_siblings,
)

Draft6Validator = create(
    meta_schema=_utils.load_schema("draft6"),
    validators={
        "$ref": _validators.ref,
        "additionalItems": _validators.additionalItems,
        "additionalProperties": _validators.additionalProperties,
        "allOf": _validators.allOf,
        "anyOf": _validators.anyOf,
        "const": _validators.const,
        "contains": _legacy_validators.contains_draft6_draft7,
        "dependencies": _legacy_validators.dependencies_draft4_draft6_draft7,
        "enum": _validators.enum,
        "exclusiveMaximum": _validators.exclusiveMaximum,
        "exclusiveMinimum": _validators.exclusiveMinimum,
        "format": _validators.format,
        "items": _legacy_validators.items_draft6_draft7_draft201909,
        "maxItems": _validators.maxItems,
        "maxLength": _validators.maxLength,
        "maxProperties": _validators.maxProperties,
        "maximum": _validators.maximum,
        "minItems": _validators.minItems,
        "minLength": _validators.minLength,
        "minProperties": _validators.minProperties,
        "minimum": _validators.minimum,
        "multipleOf": _validators.multipleOf,
        "not": _validators.not_,
        "oneOf": _validators.oneOf,
        "pattern": _validators.pattern,
        "patternProperties": _validators.patternProperties,
        "properties": _validators.properties,
        "propertyNames": _validators.propertyNames,
        "required": _validators.required,
        "type": _validators.type,
        "uniqueItems": _validators.uniqueItems,
    },
    type_checker=_types.draft6_type_checker,
    version="draft6",
    applicable_validators=_legacy_validators.ignore_ref_siblings,
)

Draft7Validator = create(
    meta_schema=_utils.load_schema("draft7"),
    validators={
        "$ref": _validators.ref,
        "additionalItems": _validators.additionalItems,
        "additionalProperties": _validators.additionalProperties,
        "allOf": _validators.allOf,
        "anyOf": _validators.anyOf,
        "const": _validators.const,
        "contains": _legacy_validators.contains_draft6_draft7,
        "dependencies": _legacy_validators.dependencies_draft4_draft6_draft7,
        "enum": _validators.enum,
        "exclusiveMaximum": _validators.exclusiveMaximum,
        "exclusiveMinimum": _validators.exclusiveMinimum,
        "format": _validators.format,
        "if": _validators.if_,
        "items": _legacy_validators.items_draft6_draft7_draft201909,
        "maxItems": _validators.maxItems,
        "maxLength": _validators.maxLength,
        "maxProperties": _validators.maxProperties,
        "maximum": _validators.maximum,
        "minItems": _validators.minItems,
        "minLength": _validators.minLength,
        "minProperties": _validators.minProperties,
        "minimum": _validators.minimum,
        "multipleOf": _validators.multipleOf,
        "not": _validators.not_,
        "oneOf": _validators.oneOf,
        "pattern": _validators.pattern,
        "patternProperties": _validators.patternProperties,
        "properties": _validators.properties,
        "propertyNames": _validators.propertyNames,
        "required": _validators.required,
        "type": _validators.type,
        "uniqueItems": _validators.uniqueItems,
    },
    type_checker=_types.draft7_type_checker,
    version="draft7",
    applicable_validators=_legacy_validators.ignore_ref_siblings,
)

Draft201909Validator = create(
    meta_schema=_utils.load_schema("draft2019-09"),
    vocabulary_schemas=_utils.load_vocabulary("draft2019-09"),
    validators={
        "$recursiveRef": _legacy_validators.recursiveRef,
        "$ref": _validators.ref,
        "additionalItems": _validators.additionalItems,
        "additionalProperties": _validators.additionalProperties,
        "allOf": _validators.allOf,
        "anyOf": _validators.anyOf,
        "const": _validators.const,
        "contains": _validators.contains,
        "dependentRequired": _validators.dependentRequired,
        "dependentSchemas": _validators.dependentSchemas,
        "enum": _validators.enum,
        "exclusiveMaximum": _validators.exclusiveMaximum,
        "exclusiveMinimum": _validators.exclusiveMinimum,
        "format": _validators.format,
        "if": _validators.if_,
        "items": _legacy_validators.items_draft6_draft7_draft201909,
        "maxItems": _validators.maxItems,
        "maxLength": _validators.maxLength,
        "maxProperties": _validators.maxProperties,
        "maximum": _validators.maximum,
        "minItems": _validators.minItems,
        "minLength": _validators.minLength,
        "minProperties": _validators.minProperties,
        "minimum": _validators.minimum,
        "multipleOf": _validators.multipleOf,
        "not": _validators.not_,
        "oneOf": _validators.oneOf,
        "pattern": _validators.pattern,
        "patternProperties": _validators.patternProperties,
        "properties": _validators.properties,
        "propertyNames": _validators.propertyNames,
        "required": _validators.required,
        "type": _validators.type,
        "unevaluatedItems": _validators.unevaluatedItems,
        "unevaluatedProperties": _validators.unevaluatedProperties,
        "uniqueItems": _validators.uniqueItems,
    },
    type_checker=_types.draft201909_type_checker,
    version="draft2019-09",
)

Draft202012Validator = create(
    meta_schema=_utils.load_schema("draft2020-12"),
    vocabulary_schemas=_utils.load_vocabulary("draft2020-12"),
    validators={
        "$dynamicRef": _validators.dynamicRef,
        "$ref": _validators.ref,
        "additionalItems": _validators.additionalItems,
        "additionalProperties": _validators.additionalProperties,
        "allOf": _validators.allOf,
        "anyOf": _validators.anyOf,
        "const": _validators.const,
        "contains": _validators.contains,
        "dependentRequired": _validators.dependentRequired,
        "dependentSchemas": _validators.dependentSchemas,
        "enum": _validators.enum,
        "exclusiveMaximum": _validators.exclusiveMaximum,
        "exclusiveMinimum": _validators.exclusiveMinimum,
        "format": _validators.format,
        "if": _validators.if_,
        "items": _validators.items,
        "maxItems": _validators.maxItems,
        "maxLength": _validators.maxLength,
        "maxProperties": _validators.maxProperties,
        "maximum": _validators.maximum,
        "minItems": _validators.minItems,
        "minLength": _validators.minLength,
        "minProperties": _validators.minProperties,
        "minimum": _validators.minimum,
        "multipleOf": _validators.multipleOf,
        "not": _validators.not_,
        "oneOf": _validators.oneOf,
        "pattern": _validators.pattern,
        "patternProperties": _validators.patternProperties,
        "prefixItems": _validators.prefixItems,
        "properties": _validators.properties,
        "propertyNames": _validators.propertyNames,
        "required": _validators.required,
        "type": _validators.type,
        "unevaluatedItems": _validators.unevaluatedItems,
        "unevaluatedProperties": _validators.unevaluatedProperties,
        "uniqueItems": _validators.uniqueItems,
    },
    type_checker=_types.draft202012_type_checker,
    version="draft2020-12",
)

_LATEST_VERSION = Draft202012Validator


class RefResolver(object):
    """
    Resolve JSON References.

    Arguments:

        base_uri (str):

            The URI of the referring document

        referrer:

            The actual referring document

        store (dict):

            A mapping from URIs to documents to cache

        cache_remote (bool):

            Whether remote refs should be cached after first resolution

        handlers (dict):

            A mapping from URI schemes to functions that should be used
            to retrieve them

        urljoin_cache (:func:`functools.lru_cache`):

            A cache that will be used for caching the results of joining
            the resolution scope to subscopes.

        remote_cache (:func:`functools.lru_cache`):

            A cache that will be used for caching the results of
            resolved remote URLs.

    Attributes:

        cache_remote (bool):

            Whether remote refs should be cached after first resolution
    """

    def __init__(
        self,
        base_uri,
        referrer,
        store=(),
        cache_remote=True,
        handlers=(),
        urljoin_cache=None,
        remote_cache=None,
    ):
        if urljoin_cache is None:
            urljoin_cache = lru_cache(1024)(urljoin)
        if remote_cache is None:
            remote_cache = lru_cache(1024)(self.resolve_from_url)

        self.referrer = referrer
        self.cache_remote = cache_remote
        self.handlers = dict(handlers)

        self._scopes_stack = [base_uri]
        self.store = _utils.URIDict(_store_schema_list())
        self.store.update(store)
        self.store[base_uri] = referrer

        self._urljoin_cache = urljoin_cache
        self._remote_cache = remote_cache

    @classmethod
    def from_schema(cls, schema, id_of=_id_of, *args, **kwargs):
        """
        Construct a resolver from a JSON schema object.

        Arguments:

            schema:

                the referring schema

        Returns:

            `RefResolver`
        """

        return cls(base_uri=id_of(schema), referrer=schema, *args, **kwargs)

    def push_scope(self, scope):
        """
        Enter a given sub-scope.

        Treats further dereferences as being performed underneath the
        given scope.
        """
        self._scopes_stack.append(
            self._urljoin_cache(self.resolution_scope, scope),
        )

    def pop_scope(self):
        """
        Exit the most recent entered scope.

        Treats further dereferences as being performed underneath the
        original scope.

        Don't call this method more times than `push_scope` has been
        called.
        """
        try:
            self._scopes_stack.pop()
        except IndexError:
            raise exceptions.RefResolutionError(
                "Failed to pop the scope from an empty stack. "
                "`pop_scope()` should only be called once for every "
                "`push_scope()`",
            )

    @property
    def resolution_scope(self):
        """
        Retrieve the current resolution scope.
        """
        return self._scopes_stack[-1]

    @property
    def scopes_stack_copy(self):
        """
        Retrieve a copy of the stack of resolution scopes.
        """
        return self._scopes_stack.copy()

    @property
    def base_uri(self):
        """
        Retrieve the current base URI, not including any fragment.
        """
        uri, _ = urldefrag(self.resolution_scope)
        return uri

    @contextlib.contextmanager
    def in_scope(self, scope):
        """
        Temporarily enter the given scope for the duration of the context.
        """
        self.push_scope(scope)
        try:
            yield
        finally:
            self.pop_scope()

    @contextlib.contextmanager
    def resolving(self, ref):
        """
        Resolve the given ``ref`` and enter its resolution scope.

        Exits the scope on exit of this context manager.

        Arguments:

            ref (str):

                The reference to resolve
        """

        url, resolved = self.resolve(ref)
        self.push_scope(url)
        try:
            yield resolved
        finally:
            self.pop_scope()

    def _finditem(self, schema, key):
        results = []
        if isinstance(schema, dict):
            if key in schema:
                results.append(schema)

            for v in schema.values():
                if isinstance(v, dict):
                    results += self._finditem(v, key)

        return results

    def resolve_local(self, url, schema):
        """
        Resolve the given reference within the schema
        """
        uri, fragment = urldefrag(url)

        for subschema in self._finditem(schema, "$id"):
            target_uri = self._urljoin_cache(
                self.resolution_scope, subschema["$id"],
            )
            if target_uri.rstrip("/") == uri.rstrip("/"):
                if fragment:
                    subschema = self.resolve_fragment(subschema, fragment)
                return subschema

    def resolve(self, ref):
        """
        Resolve the given reference.
        """
        url = self._urljoin_cache(self.resolution_scope, ref).rstrip("/")
        local_resolve = self.resolve_local(url, self.referrer)

        if local_resolve:
            return url, local_resolve
        return url, self._remote_cache(url)

    def resolve_from_url(self, url):
        """
        Resolve the given remote URL.
        """
        url, fragment = urldefrag(url)
        try:
            document = self.store[url]
        except KeyError:
            try:
                document = self.resolve_remote(url)
            except Exception as exc:
                raise exceptions.RefResolutionError(exc)

        return self.resolve_fragment(document, fragment)

    def resolve_fragment(self, document, fragment):
        """
        Resolve a ``fragment`` within the referenced ``document``.

        Arguments:

            document:

                The referent document

            fragment (str):

                a URI fragment to resolve within it
        """

        fragment = fragment.lstrip("/")

        if fragment:
            for keyword in ["$anchor", "$dynamicAnchor"]:
                for subschema in self._finditem(document, keyword):
                    if fragment == subschema[keyword]:
                        return subschema

        # Resolve via path
        parts = unquote(fragment).split("/") if fragment else []
        for part in parts:
            part = part.replace("~1", "/").replace("~0", "~")

            if isinstance(document, Sequence):
                # Array indexes should be turned into integers
                try:
                    part = int(part)
                except ValueError:
                    pass
            try:
                document = document[part]
            except (TypeError, LookupError):
                raise exceptions.RefResolutionError(
                    f"Unresolvable JSON pointer: {fragment!r}",
                )

        return document

    def resolve_remote(self, uri):
        """
        Resolve a remote ``uri``.

        If called directly, does not check the store first, but after
        retrieving the document at the specified URI it will be saved in
        the store if :attr:`cache_remote` is True.

        .. note::

            If the requests_ library is present, ``jsonschema`` will use it to
            request the remote ``uri``, so that the correct encoding is
            detected and used.

            If it isn't, or if the scheme of the ``uri`` is not ``http`` or
            ``https``, UTF-8 is assumed.

        Arguments:

            uri (str):

                The URI to resolve

        Returns:

            The retrieved document

        .. _requests: https://pypi.org/project/requests/
        """
        try:
            import requests
        except ImportError:
            requests = None

        scheme = urlsplit(uri).scheme

        if scheme in self.handlers:
            result = self.handlers[scheme](uri)
        elif scheme in ["http", "https"] and requests:
            # Requests has support for detecting the correct encoding of
            # json over http
            result = requests.get(uri).json()
        else:
            # Otherwise, pass off to urllib and assume utf-8
            with urlopen(uri) as url:
                result = json.loads(url.read().decode("utf-8"))

        if self.cache_remote:
            self.store[uri] = result
        return result


def validate(instance, schema, cls=None, *args, **kwargs):
    """
    Validate an instance under the given schema.

        >>> validate([2, 3, 4], {"maxItems": 2})
        Traceback (most recent call last):
            ...
        ValidationError: [2, 3, 4] is too long

    :func:`validate` will first verify that the provided schema is
    itself valid, since not doing so can lead to less obvious error
    messages and fail in less obvious or consistent ways.

    If you know you have a valid schema already, especially if you
    intend to validate multiple instances with the same schema, you
    likely would prefer using the `IValidator.validate` method directly
    on a specific validator (e.g. ``Draft7Validator.validate``).


    Arguments:

        instance:

            The instance to validate

        schema:

            The schema to validate with

        cls (IValidator):

            The class that will be used to validate the instance.

    If the ``cls`` argument is not provided, two things will happen
    in accordance with the specification. First, if the schema has a
    :validator:`$schema` property containing a known meta-schema [#]_
    then the proper validator will be used. The specification recommends
    that all schemas contain :validator:`$schema` properties for this
    reason. If no :validator:`$schema` property is found, the default
    validator class is the latest released draft.

    Any other provided positional and keyword arguments will be passed
    on when instantiating the ``cls``.

    Raises:

        `jsonschema.exceptions.ValidationError` if the instance
            is invalid

        `jsonschema.exceptions.SchemaError` if the schema itself
            is invalid

    .. rubric:: Footnotes
    .. [#] known by a validator registered with
        `jsonschema.validators.validates`
    """
    if cls is None:
        cls = validator_for(schema)

    cls.check_schema(schema)
    validator = cls(schema, *args, **kwargs)
    error = exceptions.best_match(validator.iter_errors(instance))
    if error is not None:
        raise error


def validator_for(schema, default=_LATEST_VERSION):
    """
    Retrieve the validator class appropriate for validating the given schema.

    Uses the :validator:`$schema` property that should be present in the
    given schema to look up the appropriate validator class.

    Arguments:

        schema (collections.abc.Mapping or bool):

            the schema to look at

        default:

            the default to return if the appropriate validator class
            cannot be determined.

            If unprovided, the default is to return the latest supported
            draft.
    """
    if schema is True or schema is False or "$schema" not in schema:
        return default
    if schema["$schema"] not in meta_schemas:
        warn(
            (
                "The metaschema specified by $schema was not found. "
                "Using the latest draft to validate, but this will raise "
                "an error in the future."
            ),
            DeprecationWarning,
            stacklevel=2,
        )
    return meta_schemas.get(schema["$schema"], _LATEST_VERSION)
