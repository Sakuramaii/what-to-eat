"""Canonical descriptive levels; ordinal ranks exist only while computing comparisons."""
METRIC_TERMS = {
    'distance': ['远','较远','适中','较近','近'],
    'price': ['贵','较贵','适中','较便宜','便宜'],
    'taste': ['不好吃','不太好吃','一般','好吃','很好吃'],
    'portion': ['少','较少','适中','较多','多'],
    'speed': ['慢','较慢','适中','较快','快'],
}
FEEDBACK_TERMS = {
    'taste': METRIC_TERMS['taste'],
    'portion': ['很不满意','不太满意','一般','满意','很满意'],
    'value': ['不划算','不太划算','一般','划算','很划算'],
}


def description(field,value,terms):
    options = terms[field]
    if isinstance(value,str) and value in options:
        return value
    # Only for old local records and old clients. All writes use canonical text.
    if isinstance(value,int) and not isinstance(value,bool) and 1<=value<=5:
        return options[value-1]
    if isinstance(value,str) and value in ['1','2','3','4','5']:
        return options[int(value)-1]
    raise ValueError('无法识别的评价描述：'+str(field))


def metric_description(field,value):
    return description(field,value,METRIC_TERMS)


def metric_rank(field,value):
    return METRIC_TERMS[field].index(metric_description(field,value))+1


def feedback_description(field,value):
    return description(field,value,FEEDBACK_TERMS)


def feedback_rank(field,value):
    return FEEDBACK_TERMS[field].index(feedback_description(field,value))+1


def describe_plan(plan):
    plan = dict(plan)
    plan.pop('variety_score',None)  # A transient ranking weight is not part of a saved meal.
    if plan.get('rated') and isinstance(plan.get('ratings'),dict):
        plan = dict(plan,ratings={field:metric_description(field,value) for field,value in plan['ratings'].items()})
    return plan
