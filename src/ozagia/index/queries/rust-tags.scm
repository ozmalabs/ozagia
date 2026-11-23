; ADT definitions

(struct_item
    name: (type_identifier) @name) @definition.class

(enum_item
    name: (type_identifier) @name) @definition.class

(union_item
    name: (type_identifier) @name) @definition.class

; type aliases

(type_item
    name: (type_identifier) @name) @definition.class

; method definitions

(declaration_list
    (function_item
        name: (identifier) @name) @definition.method)

; trait method signatures (no body)

(declaration_list
    (function_signature_item
        name: (identifier) @name) @definition.method)

; function definitions

(function_item
    name: (identifier) @name) @definition.function

; trait definitions

(trait_item
    name: (type_identifier) @name) @definition.interface

; module definitions

(mod_item
    name: (identifier) @name) @definition.module

; macro definitions

(macro_definition
    name: (identifier) @name) @definition.macro

; constants, statics and enum variants

(const_item
    name: (identifier) @name) @definition.constant

(static_item
    name: (identifier) @name) @definition.constant

(enum_variant
    name: (identifier) @name) @definition.constant

; impl blocks establish a qualname scope for their methods without emitting a
; second definition for the implementing type. The extractor strips generics.

(impl_item
    type: (_) @scope.impl_name) @scope.impl

; references

(call_expression
    function: (identifier) @name) @reference.call

(call_expression
    function: (field_expression
        field: (field_identifier) @name)) @reference.call

; qualified calls such as Foo::bar() and crate::a::b()

(call_expression
    function: (scoped_identifier
        name: (identifier) @name) @path) @reference.call

(macro_invocation
    macro: (identifier) @name) @reference.call

; implementations

(impl_item
    trait: (type_identifier) @name) @reference.implementation

(impl_item
    type: (type_identifier) @name
    !trait) @reference.implementation
