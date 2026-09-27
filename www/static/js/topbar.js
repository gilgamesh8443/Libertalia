// For smooth transitions on the link buttons
function getMinContentWidth(el) {
    const ghost = el.cloneNode(true);
    ghost.style.cssText = 'position:absolute;visibility:hidden;width:min-content;pointer-events:none';
    document.body.appendChild(ghost);
    const width = ghost.getBoundingClientRect().width;
    document.body.removeChild(ghost);
    return width;
}
document.querySelectorAll('.topbar-link').forEach(link => {
    link.style.setProperty('--link-expanded-width', `${getMinContentWidth(link)}px`);
});