; Ozagia's bundled tags query for Kotlin.
;
; The tree-sitter-kotlin wheel ships no queries/tags.scm (verified 1.1.0), so
; this file is resolved through the registry's ``query_package: "ozagia"``
; entry rather than from the grammar package. Node and field names were checked
; against the generated ``src/node-types.json`` for tree-sitter-kotlin 1.1.0
; and the query compiles against that grammar.
;
; Syntactic hints only: no scope resolution, overload resolution or type
; inference. A qualified call captures each identifier in its receiver chain
; (for example ``users.map`` yields references to both ``users`` and ``map``);
; duplicates are occurrences, not unique symbols.

; Definitions

(class_declaration
  name: (identifier) @name) @definition.class

(object_declaration
  name: (identifier) @name) @definition.class

(companion_object
  name: (identifier) @name) @definition.class

(enum_entry
  (identifier) @name) @definition.enum_member

(function_declaration
  name: (identifier) @name) @definition.function

(property_declaration
  (variable_declaration
    (identifier) @name)) @definition.property

(type_alias
  type: (identifier) @name) @definition.type

; References

(call_expression
  (identifier) @name) @reference.call

(call_expression
  (navigation_expression
    (identifier) @name)) @reference.call

(user_type
  (identifier) @name) @reference.type
